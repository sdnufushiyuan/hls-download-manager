#!/usr/bin/env python3
"""Send deduplicated HLS service failure mail from the always-on Linux master."""
from __future__ import annotations

import argparse
from email.message import EmailMessage
import getpass
import json
import os
from pathlib import Path
import smtplib
import ssl
import subprocess
import sys
import tempfile

SERVICE = "hls-archive.service"
CONFIG = Path.home() / ".config/hls-email-alert/config.json"
STATE = Path.home() / ".local/state/hls-email-alert/state.json"
DEFAULT_SENDER = os.environ.get("HLS_ALERT_SENDER", "")
DEFAULT_RECIPIENT = os.environ.get("HLS_ALERT_RECIPIENT", "")


def write_private_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_private_json(path: Path):
    if not path.is_file():
        return None
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"私有配置权限不安全：{path}（应为当前用户所有、0600）。")
    return json.loads(path.read_text(encoding="utf-8"))


def configure(sender, recipient):
    if not all(isinstance(address, str) and '@' in address and not any(c in address for c in '\r\n')
               for address in (sender, recipient)):
        raise ValueError("请通过 --sender 和 --recipient 明确指定发件人与收件人。")
    print(f"发件人：{sender}\n收件人：{recipient}")
    print("请粘贴 QQ 邮箱 SMTP 授权码；不是 QQ 登录密码。输入不会回显。")
    authorization_code = getpass.getpass("SMTP 授权码：").strip()
    if not 8 <= len(authorization_code) <= 64 or any(char.isspace() for char in authorization_code):
        raise ValueError("授权码格式不符合要求；未保存。")
    config = {"sender": sender, "recipient": recipient,
              "smtp_host": "smtp.qq.com", "smtp_port": 465,
              "authorization_code": authorization_code}
    send_mail(config, "HLS 下载提醒测试", "这是 Linux 服务器发出的 HLS 下载故障提醒通道测试。")
    write_private_json(CONFIG, config)
    print(f"测试邮件已提交给 QQ SMTP：{recipient}")
    print(f"已将发信配置保存在 {CONFIG}（仅当前用户可读）。")


def service_status():
    result = subprocess.run(["systemctl", "--user", "show", SERVICE,
                             "--property=ActiveState,SubState,Result,ExecMainStatus,InvocationID"],
                            capture_output=True, text=True, timeout=15, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def send_mail(config, subject, body):
    message = EmailMessage()
    message["From"] = config["sender"]
    message["To"] = config["recipient"]
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"],
                          timeout=25, context=ssl.create_default_context()) as smtp:
        smtp.login(config["sender"], config["authorization_code"])
        smtp.send_message(message)


def check_once(config, previous, status, sender=send_mail):
    active, substate = status.get("ActiveState"), status.get("SubState")
    if active == "activating" and substate == "auto-restart":
        return previous, "service-auto-restarting"
    if active == "active":
        if previous.get("alert_key"):
            sender(config, "HLS 下载服务已恢复", "Linux 下载服务已重新运行。请在 VS Code 查看实时进度。")
            return {}, "recovered"
        return previous, "healthy"
    if active not in ("failed", "inactive"):
        raise RuntimeError("systemd 状态无法确认；不发送可能误导的邮件。")
    if active == "inactive" and status.get("Result") == "success":
        return previous, "clean-stop"
    key = "|".join(str(status.get(field, "")) for field in
                   ("InvocationID", "ActiveState", "Result", "ExecMainStatus"))
    if previous.get("alert_key") == key:
        return previous, "already-alerted"
    body = ("HLS 全历史下载服务需要人工检查。\n\n"
            f"服务：{SERVICE}\n"
            f"状态：{active}/{substate}\n"
            f"结果：{status.get('Result', 'unknown')}；退出码：{status.get('ExecMainStatus', 'unknown')}\n\n"
            "请在 VS Code 连接 master 后运行：\n"
            "bash '/opt/hls-download-manager/watch_hls_progress.sh'\n"
            "或查看：journalctl --user -u hls-archive.service -n 80 --no-pager\n"
            "不要在邮件中传送授权码或影像下载地址。\n")
    sender(config, "HLS 下载服务停止，需要检查", body)
    return {"alert_key": key}, "alert-sent"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--configure", action="store_true", help="交互输入 QQ 邮箱 SMTP 授权码")
    actions.add_argument("--test", action="store_true", help="发送一封测试邮件")
    actions.add_argument("--check", action="store_true", help="检查服务并按状态变化提醒")
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    parser.add_argument("--recipient", default=DEFAULT_RECIPIENT)
    args = parser.parse_args(argv)
    try:
        if args.configure:
            configure(args.sender, args.recipient)
            return 0
        config = read_private_json(CONFIG)
        if config is None:
            if args.check:
                return 0  # Timer may be installed before the user enters credentials.
            raise RuntimeError("邮件尚未配置；请先执行 --configure。")
        if not all(isinstance(config.get(key), str) and '@' in config[key] and not any(c in config[key] for c in '\r\n')
                   for key in ('sender', 'recipient')) or \
                config.get("smtp_host") != "smtp.qq.com" or config.get("smtp_port") != 465 or \
                not config.get("authorization_code"):
            raise RuntimeError("邮件配置与本任务预期地址不符，已停止发送。")
        if args.test:
            send_mail(config, "HLS 下载提醒测试", "这是 Linux 服务器发出的 HLS 下载故障提醒通道测试。")
            print("测试邮件已提交给 QQ SMTP。")
            return 0
        previous = read_private_json(STATE) or {}
        updated, outcome = check_once(config, previous, service_status())
        if updated != previous:
            write_private_json(STATE, updated)
        print(f"HLS 邮件巡检：{outcome}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError,
            smtplib.SMTPException, json.JSONDecodeError) as error:
        # SMTP exceptions may include server/account details; never print raw text.
        print(f"HLS 邮件巡检未完成（{type(error).__name__}）；下轮会重试。", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
