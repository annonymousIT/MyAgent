"""Gmail 受信メール読み取り（IMAP・アプリパスワード方式・ADR-0044）。

OAuth不要・読み取り専用・標準ライブラリ(imaplib)のみ。カレンダーの iCal 方式と同じ思想：
Gmailで2段階認証をON→「アプリパスワード」を発行し、.env に2行入れるだけ。
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   （16桁・空白は無視する）

設計:
- 落ちない: 未設定・認証失敗・接続失敗なら空を返す。エージェント本体は止めない。
- 安い/privacy: 取得は差出人・件名・日付の「ヘッダのみ」（本文は取らない）。要約はLLM。
- オンデマンド: 毎ターンではなくツール(get_mail)経由で「メール来てる？」の時だけ取りに行く。
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr


def _dec(raw: str) -> str:
    """MIMEエンコードされたヘッダ（=?UTF-8?...?=）を読める文字列に。"""
    try:
        parts = decode_header(raw or "")
        out = []
        for txt, enc in parts:
            if isinstance(txt, bytes):
                out.append(txt.decode(enc or "utf-8", "replace"))
            else:
                out.append(txt)
        return "".join(out).strip()
    except Exception:
        return raw or ""


def recent_unread(limit: int = 8):
    """直近の未読メールを [(差出人名, 件名, "M/D HH:MM"), ...] で返す（新しい順）。失敗時は []。

    本文は取得しない（ヘッダのみ＝privacy/コスト）。未設定・認証失敗は静かに []。
    """
    addr = os.environ.get("GMAIL_ADDRESS", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()  # アプリPWは空白付き表示なので除去
    if not addr or not pw:
        return []
    M = None
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        M.login(addr, pw)
        M.select("INBOX", readonly=True)  # readonly＝既読にしない
        typ, data = M.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-limit:]
        out = []
        for i in reversed(ids):  # 新しい順
            typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            frm = _dec(msg.get("From", ""))
            name, eaddr = parseaddr(frm)
            who = _dec(name) or eaddr or frm
            subj = _dec(msg.get("Subject", "")) or "(件名なし)"
            when = ""
            try:
                dt = parsedate_to_datetime(msg.get("Date", ""))
                if dt is not None:
                    dt = dt.astimezone()
                    when = f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"
            except Exception:
                pass
            out.append((who, subj, when))
        return out
    except Exception:
        return []
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass
