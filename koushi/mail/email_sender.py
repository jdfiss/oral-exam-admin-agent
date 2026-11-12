"""最小自動寄信模組 — Gmail SMTP
讀取 MAIL_SENDER 和 MAIL_PASSWORD：依序找 koushi/.env、專案根目錄 .env；
兩檔都沒有時改讀同名環境變數（排程主機可直接設環境變數，不必放檔案）。
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"
ENV_PATHS = (ENV_PATH, ENV_PATH.parent.parent / ".env")
_REQUIRED = ("MAIL_SENDER", "MAIL_PASSWORD")


def _parse(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"')
    return result


def _load_env() -> dict[str, str]:
    path = next((p for p in ENV_PATHS if p.is_file()), None)
    if path is None:
        result = {k: v for k, v in os.environ.items() if k.startswith("MAIL_")}
        if all(result.get(k) for k in _REQUIRED):
            return result
        raise FileNotFoundError(
            "找不到寄信設定檔：" + "、".join(str(p) for p in ENV_PATHS) + "\n"
            "請在其中一處建立 .env 並填入兩行（或設定同名環境變數）：\n"
            "    MAIL_SENDER=你的Gmail\n"
            "    MAIL_PASSWORD=應用程式密碼（16碼，非登入密碼）\n"
            ".env 已在 .gitignore 內，不會被推上 GitHub。"
        )
    result = _parse(path)
    missing = [k for k in _REQUIRED if not result.get(k)]
    if missing:
        raise ValueError(f"{path} 缺少設定：{'、'.join(missing)}")
    return result


def sender_address() -> str:
    """寄件人信箱。測試時 --to me 會寄回這個地址。"""
    return _load_env()["MAIL_SENDER"]


def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    attachments: list[str] | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | tuple[str, ...] | None = None,
) -> bool:
    env = _load_env()
    sender = env["MAIL_SENDER"]
    password = env["MAIL_PASSWORD"]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:  # 催辦信接回原信串，教授回覆時信箱會帶上原信／催辦信的 Message-ID
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    if cc:
        msg["Cc"] = cc

    msg.attach(MIMEText(body, "plain", "utf-8"))
    for path in attachments or []:
        file = Path(path)
        part = MIMEApplication(file.read_bytes())
        part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", file.name))
        msg.attach(part)

    # To／Cc 可用「, 」串接多位（如附件6 提醒寄給該場全部口試委員）
    recipients = [a.strip() for field in (to, cc) if field for a in field.split(",") if a.strip()]

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())

    print(f"✓ 信件已寄出 → {to}")
    return True


if __name__ == "__main__":
    # 自我測試：寄給自己，不對外寄信。
    address = sender_address()
    send_email(
        to=address,
        subject="[測試] 口試 agent 自動寄信測試",
        body="這是一封自動寄出的測試信件。\n"
             "如果你收到這封信，代表寄信模組運作正常。\n\n"
             "— 口試行政自動化",
    )
