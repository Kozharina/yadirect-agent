"""Operator-facing render layer for ``yadirect-agent notify setup telegram`` (M18 slice 4).

Owns the Russian prompt / status / error text and the orchestration
of the 5-step wizard. Delegates wire concerns to
``services.notify.setup_wizard`` (validate token, capture chat_id),
storage to ``auth.telegram_keychain``, and the test send to
``services.notify.telegram``.

Returns an exit code (instead of raising typer.Exit) so the wizard
is easier to unit-test — typer's exit semantics are surfaced one
layer up in ``cli/main.py``.

All operator-facing text is in Russian per CLAUDE.md
``<language_conventions>`` — target persona Anna is a non-developer
media-buyer in Russia. The structlog event names and exception
classes stay English (machine identifiers).
"""

from __future__ import annotations

import typer
from pydantic import SecretStr
from rich.console import Console

from ..auth.telegram_keychain import KeyringTelegramStore
from ..models.health import Severity
from ..models.notification import Notification
from ..services.notify.setup_wizard import (
    ChatIdTimeoutError,
    TokenInvalidError,
    await_first_chat_id,
    validate_telegram_token,
)
from ..services.notify.telegram import TelegramSink


def render_telegram_reset(out: Console) -> None:
    """Delete the keychain entry and report success.

    Idempotent — ``KeyringTelegramStore.delete`` swallows the
    "no record" case, so running ``--reset`` on a fresh install
    or twice in a row both exit 0 cleanly.
    """
    KeyringTelegramStore().delete()
    out.print(
        "[green]✓ Telegram-настройки удалены из OS keychain.[/green]\n"
        "[dim]Если бот вам больше не нужен, отзовите его токен через "  # noqa: RUF001
        "@BotFather (/revoke).[/dim]"
    )


async def run_telegram_setup_wizard(
    *,
    out: Console,
    err: Console,
    chat_id_timeout_s: float = 120.0,
) -> int:
    """Run the 5-step interactive wizard. Returns exit code (0 / 1).

    Layout:
    - out (stdout): operator prompts, step headers, success messages.
    - err (stderr): all error messages so cron-friendly redirects
      separate them from the success log cleanly.

    Step-by-step rather than monolithic so a future MCP-tool wrapper
    can drive a subset (e.g. just the chat-id capture if the operator
    already has a token from an earlier wizard run).
    """

    # ------------------------------------------------------------------
    # Step 1: BotFather instructions.
    # ------------------------------------------------------------------
    out.print("\n[bold]Настройка Telegram-уведомлений[/bold]\n")
    out.print(
        "[bold]Шаг 1 из 5[/bold]: создайте бота через @BotFather.\n"
        "1. Откройте Telegram и найдите пользователя [bold]@BotFather[/bold].\n"
        "2. Отправьте ему команду [bold]/newbot[/bold].\n"
        "3. Введите имя бота (например, «Yadirect Alerts»).\n"
        "4. Введите username бота (должен заканчиваться на [bold]_bot[/bold]).\n"
        "5. BotFather пришлёт сообщение с токеном вида "  # noqa: RUF001
        "[dim]1234567890:AAE-...[/dim] — скопируйте его.\n"  # noqa: RUF001
    )

    # ------------------------------------------------------------------
    # Step 2: token prompt.
    # ------------------------------------------------------------------
    out.print("[bold]Шаг 2 из 5[/bold]: вставьте токен бота ниже.\n")
    bot_token = typer.prompt("Bot token", hide_input=True).strip()
    if not bot_token:
        err.print("[red]Пустой токен — отмена.[/red]")
        return 1

    # ------------------------------------------------------------------
    # Step 3: validate via Bot API getMe.
    # ------------------------------------------------------------------
    out.print("\n[bold]Шаг 3 из 5[/bold]: проверяю токен через Bot API...")
    try:
        bot_info = await validate_telegram_token(bot_token)
    except TokenInvalidError as exc:
        err.print(
            "[red]Telegram отклонил токен.[/red]\n"
            "[dim]Возможные причины: токен скопирован не полностью, "
            "бот удалён через @BotFather, нет доступа к api.telegram.org "
            "(в России может потребоваться VPN на хосте). "
            f"Подробности (для отладки): {exc}[/dim]"
        )
        return 1
    out.print(f"[green]✓ Бот @{bot_info.username} найден.[/green]")

    # ------------------------------------------------------------------
    # Step 4: capture chat_id.
    # ------------------------------------------------------------------
    out.print(
        f"\n[bold]Шаг 4 из 5[/bold]: получите chat_id.\n"
        f"1. Откройте ваш бот: [link=https://t.me/{bot_info.username}]"
        f"https://t.me/{bot_info.username}[/link]\n"
        f"2. Нажмите [bold]Start[/bold] (или отправьте любое сообщение).\n"
        f"3. Возвращайтесь сюда — wizard поймает chat_id автоматически.\n"
        f"\nОжидаю сообщение от вас (до {chat_id_timeout_s:.0f} секунд)..."  # noqa: RUF001
    )
    try:
        chat_id = await await_first_chat_id(
            bot_token,
            timeout_s=chat_id_timeout_s,
        )
    except ChatIdTimeoutError:
        err.print(
            "[yellow]Не дождались сообщения за отведённое время.[/yellow]\n"  # noqa: RUF001
            "[dim]Запустите команду снова и отправьте боту любое сообщение "
            "в течение пары минут.[/dim]"
        )
        return 1
    out.print(f"[green]✓ Получен chat_id: {chat_id}[/green]")

    # ------------------------------------------------------------------
    # Step 5: save to keychain + send test notification.
    # ------------------------------------------------------------------
    out.print("\n[bold]Шаг 5 из 5[/bold]: сохраняю и отправляю тестовое сообщение...")
    KeyringTelegramStore().save(bot_token=bot_token, chat_id=chat_id)
    out.print("[green]✓ Сохранено в OS keychain.[/green]")

    sink = TelegramSink(bot_token=SecretStr(bot_token), chat_id=chat_id)
    test_notification = Notification(
        severity=Severity.INFO,
        title="yadirect-agent: настройка завершена",
        body=("Канал готов. Алёрты от health-check и плановые подтверждения будут приходить сюда."),
    )
    try:
        await sink.send(test_notification)
    except Exception as exc:
        # Token saved + chat_id captured, but the test send hit a
        # transient issue (bot was blocked between /start and the
        # test, Telegram had a 5xx burst, etc). Keep the keychain
        # entry — operator can re-run ``notify test`` later after
        # fixing the immediate issue. Discarding the entry would
        # force them to redo the whole wizard for a recoverable
        # failure.
        err.print(
            "[yellow]Не удалось отправить тестовое сообщение.[/yellow]\n"  # noqa: RUF001
            "[dim]Учётные данные сохранены в keychain. Когда исправите "
            "проблему (разблокируйте бота, проверьте сеть), запустите "
            f"`yadirect-agent notify test`. Подробности: {exc}[/dim]"
        )
        return 1

    out.print(
        "\n[green]✓ Готово![/green] Откройте Telegram и проверьте, что пришло "
        "сообщение [italic]«настройка завершена»[/italic].\n"
        "[dim]Теперь команда `yadirect-agent health` будет автоматически "
        "присылать сводки сюда. Управлять каналом: "
        "`yadirect-agent notify test` (повторная проверка), "
        "`yadirect-agent notify setup telegram --reset` (удалить keychain).[/dim]"
    )
    return 0


__all__ = [
    "render_telegram_reset",
    "run_telegram_setup_wizard",
]
