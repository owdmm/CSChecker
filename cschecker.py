import datetime
import difflib
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)

REPO_ACTIONS_URL = os.environ.get("REPO_URL", "") + "/actions"

FAIL_THRESHOLD = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}

TARGETS = [
    {
        "key": "site_a",
        "url": "https://www.kooza.jp/outline-osaka.html",
        "label": "対象ページA",
    },
    {
        "key": "site_b",
        "url": "https://www.kooza.jp/",
        "label": "対象ページB",
    },
]

KEYWORDS = ["先行販売", "先行受付", "一般販売", "発売", "販売開始", "予約開始", "抽選"]


def extract_full_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text("\n").splitlines()]
    return "\n".join(l for l in lines if l)


def fetch(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.content


def send_mail(subject: str, body: str) -> None:
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("NOTIFY_EMAIL", sender)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [to_addr], msg.as_string())


def safe_error_summary(exc: Exception) -> str:
    """Error summary for public logs: no URLs or response bodies that could name the target."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def get_failcount(key: str) -> int:
    f = STATE_DIR / f"{key}.failcount"
    return int(f.read_text()) if f.exists() else 0


def set_failcount(key: str, n: int) -> None:
    (STATE_DIR / f"{key}.failcount").write_text(str(n), encoding="utf-8")


def check_target(target: dict) -> tuple[bool, bool]:
    """Returns (content_changed, is_failing)."""
    state_file = STATE_DIR / f"{target['key']}.txt"
    fail_count = get_failcount(target["key"])

    try:
        raw = fetch(target["url"])
        text = extract_full_text(raw)
        if not text:
            raise ValueError("extracted text is empty")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] fetch/parse failed for {target['key']}: {safe_error_summary(exc)}")
        fail_count += 1
        set_failcount(target["key"], fail_count)
        if fail_count == FAIL_THRESHOLD:
            send_mail(
                f"⚠️ {target['label']} の取得エラーが続いています",
                f"{target['label']} ({target['url']}) の取得に{FAIL_THRESHOLD}回連続で失敗しました。\n\n"
                f"直近のエラー内容: {exc}\n\n"
                f"実行ログを確認してください: {REPO_ACTIONS_URL}\n",
            )
            print(f"[ALERT] {target['key']} failure alert sent")
        return False, True

    if fail_count >= FAIL_THRESHOLD:
        send_mail(
            f"✅ {target['label']} が復旧しました",
            f"{target['label']} ({target['url']}) の取得が正常に戻りました。\n",
        )
        print(f"[RECOVERED] {target['key']}")
    set_failcount(target["key"], 0)

    prev = state_file.read_text(encoding="utf-8") if state_file.exists() else None
    state_file.write_text(text, encoding="utf-8")

    if prev is None:
        print(f"[INIT] baseline saved for {target['key']}")
        return False, False

    if text == prev:
        print(f"[NOCHANGE] {target['key']}")
        return False, False

    new_keywords = [k for k in KEYWORDS if k in text and k not in prev]
    diff = "\n".join(
        difflib.unified_diff(
            prev.splitlines(), text.splitlines(), lineterm="", fromfile="変更前", tofile="変更後"
        )
    )

    urgent = "🎫【要チェック】" if new_keywords else "【更新】"
    subject = f"{urgent}{target['label']} が更新されました"
    body = (
        f"{target['label']} ({target['url']}) の内容が変わりました。\n\n"
        f"新たに検出されたキーワード: {', '.join(new_keywords) if new_keywords else '(なし・念のため通知)'}\n\n"
        "---- 差分 ----\n"
        f"{diff[:6000]}\n\n"
        f"ページを直接確認してください: {target['url']}\n"
    )
    send_mail(subject, body)
    print(f"[CHANGE] {target['key']} changed, email sent (keywords: {new_keywords})")
    return True, False


def send_heartbeat(critical_failures: list[str]) -> None:
    last_checked_file = STATE_DIR / "last_checked.txt"
    last_checked = (
        last_checked_file.read_text(encoding="utf-8") if last_checked_file.exists() else "unknown"
    )
    status = (
        "正常に動作しています(まだ大きな変化はありません)"
        if not critical_failures
        else f"一部の監視対象でエラーが続いています: {', '.join(critical_failures)}"
    )
    body = (
        "定期監視ツールからの週次の生存確認メールです。\n\n"
        f"状態: {status}\n"
        f"最終チェック時刻(UTC): {last_checked}\n\n"
        "このメールが来なくなった場合は、実行が止まっている可能性があるので、\n"
        f"リポジトリのActionsタブを確認してください: {REPO_ACTIONS_URL}\n"
    )
    send_mail("📅 週次の生存確認", body)
    print("[HEARTBEAT] sent")


def main() -> None:
    if os.environ.get("TEST_EMAIL") == "true":
        send_mail(
            "✅ 疎通確認メール",
            "このメールが届いていれば、GitHub ActionsからGmail経由での通知は正常に動作しています。",
        )
        print("[TEST] test email sent")
        return

    any_change = False
    critical_failures: list[str] = []
    for target in TARGETS:
        try:
            changed, failing = check_target(target)
            any_change = any_change or changed
            if failing:
                critical_failures.append(target["key"])
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {target['key']}: {exc}")
            critical_failures.append(target["key"])

    # keeps the repo "active" so GitHub doesn't auto-disable the schedule after 60 idle days
    (STATE_DIR / "last_checked.txt").write_text(
        datetime.datetime.now(datetime.timezone.utc).isoformat(), encoding="utf-8"
    )

    if os.environ.get("HEARTBEAT") == "true":
        send_heartbeat(critical_failures)

    print("any_change:", any_change, "critical_failures:", critical_failures)

    if critical_failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
