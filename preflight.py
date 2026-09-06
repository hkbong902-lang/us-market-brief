# -*- coding: utf-8 -*-
"""Secrets 점검 스크립트.

값 자체는 절대 출력하지 않는다. 길이·형태·실제 인증 결과만 보고한다.
(공개 저장소에서는 Actions 로그도 공개되므로 값의 일부도 찍지 않는다.)

사용: GitHub Actions의 'Check Secrets' 워크플로를 수동 실행.
"""

import os
import re
import sys

import requests

# 윈도우 콘솔(cp949)에서 한국어·기호 출력이 UnicodeEncodeError로 죽지 않도록 한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


ok_all = True


def report(label, ok, detail=""):
    global ok_all
    if not ok:
        ok_all = False
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))


def shape(name, pattern, required=True):
    """환경변수의 형태만 검사하고 (원본값, 정리된 값)을 돌려준다."""
    raw = os.environ.get(name, "")
    if not raw:
        report(f"{name} 등록", not required, "값이 비어 있음")
        return None
    val = raw.strip()
    if val != raw:
        report(f"{name} 앞뒤 공백", False, "앞뒤에 공백/줄바꿈이 있음 → 다시 등록 필요")
    if "\n" in val:
        report(f"{name} 줄바꿈", False,
               f"값에 줄바꿈이 {val.count(chr(10))}개 있음 → 설명문을 붙여넣었을 가능성")
        return None
    good = bool(re.fullmatch(pattern, val))
    report(f"{name} 형태", good, f"길이 {len(val)}자" + ("" if good else f", 기대 형태와 다름"))
    return val if good else None


print("=" * 60)
print("1) TELEGRAM_BOT_TOKEN")
token = shape("TELEGRAM_BOT_TOKEN", r"\d{6,12}:[A-Za-z0-9_-]{30,}")
if token:
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=30)
        body = r.json()
        if body.get("ok"):
            report("봇 인증", True, f"@{body['result'].get('username')} 으로 확인됨")
        else:
            report("봇 인증", False,
                   f"{body.get('error_code')} {body.get('description')} → BotFather에서 토큰 재확인")
    except Exception as e:
        report("봇 인증", False, f"요청 실패: {type(e).__name__}")

print()
print("2) TELEGRAM_CHAT_ID")
chat_id = shape("TELEGRAM_CHAT_ID", r"-?\d{5,20}")
if token and chat_id:
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getChat",
                         params={"chat_id": chat_id}, timeout=30)
        body = r.json()
        if body.get("ok"):
            report("대화방 확인", True, f"type={body['result'].get('type')}")
        else:
            report("대화방 확인", False,
                   f"{body.get('error_code')} {body.get('description')} → 봇에게 메시지를 한 번 보낸 뒤 getUpdates로 재확인")
    except Exception as e:
        report("대화방 확인", False, f"요청 실패: {type(e).__name__}")

print()
print("3) ANTHROPIC_API_KEY (선택)")
key = shape("ANTHROPIC_API_KEY", r"sk-ant-[A-Za-z0-9_\-]{20,}", required=False)
if key:
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"),
                  "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
            timeout=60,
        )
        if r.status_code == 200:
            report("API 키 인증", True, "정상")
        else:
            err = r.json().get("error", {})
            report("API 키 인증", False,
                   f"{r.status_code} {err.get('type')}: {err.get('message')}")
    except Exception as e:
        report("API 키 인증", False, f"요청 실패: {type(e).__name__}")

print("=" * 60)
print("모두 통과" if ok_all else "실패 항목이 있습니다. 위 FAIL 줄을 확인하세요.")
sys.exit(0 if ok_all else 1)
