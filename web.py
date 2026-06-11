"""MyAgent 動作確認用Webアプリ（テキスト入力・音声なし）。

ブラウザで「入力 → 返答 → どのツールが動いたか」を確認するための開発用UI。
日本語入力もきれいな見た目もブラウザ任せ（tkinterのTk8.5問題を回避）。標準ライブラリのみ。

起動: source .env && source .venv/bin/activate && python web.py
       → ブラウザで http://localhost:8765 を開く
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import core

PORT = 8765

PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyAgent</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1f242e; --line:#2a313d;
    --me:#2563eb; --bot:#222834; --text:#e8ebf0; --muted:#8a93a3;
    --accent:#f5b945; --ok:#43c478;
  }
  *{box-sizing:border-box}
  body{
    margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,"Hiragino Sans","Helvetica Neue",sans-serif;
    background:radial-gradient(1200px 800px at 70% -10%,#1b2230,#0f1115); color:var(--text);
  }
  .app{
    width:min(680px,94vw); height:min(86vh,820px); display:flex; flex-direction:column;
    background:var(--panel); border:1px solid var(--line); border-radius:18px; overflow:hidden;
    box-shadow:0 24px 60px rgba(0,0,0,.45);
  }
  header{
    padding:16px 20px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px;
    background:linear-gradient(180deg,#1b212c,#171a21);
  }
  header .dot{width:10px;height:10px;border-radius:50%;background:var(--ok);box-shadow:0 0 10px var(--ok)}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  header .sub{font-size:12px;color:var(--muted);margin-left:auto}
  .log{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
  .row{display:flex;flex-direction:column;max-width:82%}
  .row.me{align-self:flex-end;align-items:flex-end}
  .row.bot{align-self:flex-start;align-items:flex-start}
  .bubble{padding:11px 15px;border-radius:16px;font-size:15px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
  .me .bubble{background:var(--me);border-bottom-right-radius:5px}
  .bot .bubble{background:var(--bot);border:1px solid var(--line);border-bottom-left-radius:5px}
  .name{font-size:11px;color:var(--muted);margin:0 4px 4px}
  .action{
    align-self:flex-start;font-size:12.5px;color:var(--accent);background:rgba(245,185,69,.08);
    border:1px solid rgba(245,185,69,.25);padding:6px 11px;border-radius:10px;font-family:ui-monospace,Menlo,monospace;
  }
  .typing{display:flex;gap:4px;padding:12px 15px;background:var(--bot);border:1px solid var(--line);border-radius:16px;border-bottom-left-radius:5px}
  .typing span{width:7px;height:7px;border-radius:50%;background:var(--muted);animation:b 1.2s infinite}
  .typing span:nth-child(2){animation-delay:.2s}.typing span:nth-child(3){animation-delay:.4s}
  @keyframes b{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-4px)}}
  .composer{padding:14px 16px;border-top:1px solid var(--line);background:var(--panel2)}
  .inrow{display:flex;gap:10px}
  .inrow input{
    flex:1;background:#0f131a;border:1px solid var(--line);color:var(--text);
    padding:13px 15px;border-radius:12px;font-size:15px;outline:none;
  }
  .inrow input:focus{border-color:var(--me)}
  .inrow button{
    background:var(--me);color:#fff;border:none;padding:0 22px;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;
  }
  .inrow button:disabled{opacity:.45;cursor:default}
  .opt{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:13px;color:var(--muted);cursor:pointer;user-select:none}
  .opt input{width:16px;height:16px;accent-color:var(--accent)}
</style>
</head>
<body>
  <div class="app">
    <header>
      <span class="dot"></span>
      <h1>MyAgent — 動作確認</h1>
      <span class="sub">テキスト入力・無音</span>
    </header>
    <div class="log" id="log"></div>
    <div class="composer">
      <div class="inrow">
        <input id="msg" placeholder="話しかける（例：github開いて / 課題見よ / おはよ）" autocomplete="off" autofocus>
        <button id="send">送信</button>
      </div>
      <label class="opt"><input type="checkbox" id="dry" checked> ドライラン（実際には開かず、選ばれたツールと返答だけ確認）</label>
    </div>
  </div>
<script>
const log=document.getElementById('log'), input=document.getElementById('msg'),
      btn=document.getElementById('send'), dry=document.getElementById('dry');

function el(cls,html){const d=document.createElement('div');d.className=cls;if(html!==undefined)d.innerHTML=html;return d}
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function bubble(who,text){
  const row=el('row '+who);
  row.appendChild(el('name', who==='me'?'あなた':'MyAgent'));
  const b=el('bubble'); b.textContent=text; row.appendChild(b);
  log.appendChild(row); log.scrollTop=log.scrollHeight;
}
function action(a){
  const icon = a.kind==='dry' ? '🔍 選択' : '⚙ 実行';
  const r=el('action'); r.textContent=`${icon}: ${a.label}` + (a.kind==='run'?` → ${a.result}`:'');
  log.appendChild(r); log.scrollTop=log.scrollHeight;
}
let typingEl=null;
function showTyping(){typingEl=el('typing','<span></span><span></span><span></span>');log.appendChild(typingEl);log.scrollTop=log.scrollHeight}
function hideTyping(){if(typingEl){typingEl.remove();typingEl=null}}

async function send(){
  const text=input.value.trim(); if(!text) return;
  input.value=''; bubble('me',text);
  btn.disabled=true; input.disabled=true; showTyping();
  try{
    const res=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text, dry_run:dry.checked})});
    const data=await res.json();
    hideTyping();
    if(data.error){ bubble('bot','⚠ エラー: '+data.error); }
    else{ (data.actions||[]).forEach(action); bubble('bot', data.reply); }
  }catch(e){ hideTyping(); bubble('bot','⚠ 通信エラー: '+e); }
  btn.disabled=false; input.disabled=false; input.focus();
}
btn.onclick=send;
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing)send()});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    client = None
    persona = ""
    history = []  # 会話履歴（Step5(a)）。単一ユーザー前提でプロセス内に保持

    def log_message(self, *args):  # アクセスログを黙らせる
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/chat":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = (payload.get("text") or "").strip()
            dry = bool(payload.get("dry_run", True))
            if not text:
                self._send(200, json.dumps({"actions": [], "reply": "（何か入力してください）"}), "application/json")
                return
            # 会話のリセット（記憶を消す）
            if text in ("リセット", "履歴クリア", "忘れて", "リセットして"):
                Handler.history = []
                self._send(200, json.dumps({"actions": [], "reply": "会話の記憶をリセットしました。"}, ensure_ascii=False), "application/json; charset=utf-8")
                return
            result = core.run_turn(self.client, self.persona, text, dry_run=dry, history=Handler.history)
            Handler.history = result.pop("history", Handler.history)  # 次ターンへ持ち越し（clientには返さない）
            self._send(200, json.dumps(result, ensure_ascii=False), "application/json; charset=utf-8")
        except Exception as e:
            self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), "application/json; charset=utf-8")


def main():
    Handler.client = core.build_client()
    Handler.persona = core.load_persona()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"MyAgent web UI 起動：http://localhost:{PORT}  （Ctrl+C で停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")


if __name__ == "__main__":
    main()
