"""MyAgent 動作確認用Webアプリ（テキスト入力・音声なし）。

ブラウザで「入力 → 返答 → どのツールが動いたか」を確認するための開発用UI。
日本語入力もきれいな見た目もブラウザ任せ（tkinterのTk8.5問題を回避）。標準ライブラリのみ。

起動: source .env && source .venv/bin/activate && python web.py
       → ブラウザで http://localhost:8765 を開く
"""

from __future__ import annotations

import calendar
import datetime
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core
import speak as speak_mod
import tools

# 実測コストの記録（1ターン1行のJSONL・円）。月500円目標の監視用（ADR-0033）。gitignore対象。
COSTS_PATH = Path(__file__).parent / "costs.jsonl"


def _log_cost(usage: dict) -> dict:
    """今ターンの実測コストを記録し、今日/今月/月ペース(円)を返す。"""
    now = datetime.datetime.now()
    with open(COSTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                            "yen": usage["yen"], "calls": usage["calls"]}) + "\n")
    today = month = 0.0
    d_pref, m_pref = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    for line in open(COSTS_PATH, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["ts"].startswith(m_pref):
            month += r["yen"]
            if r["ts"].startswith(d_pref):
                today += r["yen"]
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    pace = month / now.day * days_in_month  # このペースで使うと月いくらか
    return {"turn": round(usage["yen"], 2), "today": round(today, 1),
            "month": round(month, 1), "pace": round(pace)}

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
  .judge{align-self:flex-start;max-width:88%;margin-top:-4px;display:flex;flex-direction:column;gap:6px}
  .jrow{background:rgba(245,185,69,.06);border:1px solid rgba(245,185,69,.22);border-left:3px solid var(--accent);
    border-radius:10px;padding:8px 11px;font-size:12.5px;color:var(--text)}
  .jhead{margin-bottom:3px}
  .jhead b{color:var(--accent)}
  .jmode{float:right;color:var(--muted);font-size:11.5px}
  .jline{color:var(--muted);line-height:1.5;word-break:break-word}
  .jline code{font-family:ui-monospace,Menlo,monospace;color:#cdd5e2;background:rgba(255,255,255,.04);padding:1px 5px;border-radius:5px}
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
      <span class="sub" id="cost"></span>
      <span class="sub">テキスト入力（音声の代わり）</span>
    </header>
    <div class="log" id="log"></div>
    <div class="composer">
      <div class="inrow">
        <input id="msg" placeholder="話しかける（例：github開いて / 課題見よ / おはよ）" autocomplete="off" autofocus>
        <button id="send">送信</button>
      </div>
      <label class="opt"><input type="checkbox" id="dry" checked> ドライラン（ON=実行せず判断だけ確認 / OFFで実際に実行）</label>
      <label class="opt"><input type="checkbox" id="aloud"> 読み上げ（返答を音声で。VOICEVOX起動中ならVOICEVOX、無ければOS標準）</label>
    </div>
  </div>
<script>
const log=document.getElementById('log'), input=document.getElementById('msg'),
      btn=document.getElementById('send'), dry=document.getElementById('dry'),
      aloud=document.getElementById('aloud');

function el(cls,html){const d=document.createElement('div');d.className=cls;if(html!==undefined)d.innerHTML=html;return d}
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function bubble(who,text){
  const row=el('row '+who);
  row.appendChild(el('name', who==='me'?'あなた':'MyAgent'));
  const b=el('bubble'); b.textContent=text; row.appendChild(b);
  log.appendChild(row); log.scrollTop=log.scrollHeight;
}
const TOOLJP={
  open_site:'サイトを開く', launch_app:'アプリを起動', run_system:'システム操作',
  close_app:'閉じる', get_weather:'天気を取得', play_media:'動画/音楽を開く',
  manage_window:'ウィンドウ配置', fetch_page:'ページ本文を取得', web_search:'Web検索', open_url:'URLを開く',
  remember:'永続記憶に保存', add_schedule:'予定を登録', forget:'記憶を削除'
};
// 吹き出しの下に「どう判断して・何を実行したか」を出す（テスト用ログ）
function judge(actions){
  const box=el('judge');
  if(!actions||!actions.length){
    const r=el('jrow'); r.innerHTML='<div class="jhead">🧠 判断: <b>操作なし</b> <span class="jmode">会話で対応</span></div>';
    box.appendChild(r); log.appendChild(box); log.scrollTop=log.scrollHeight; return;
  }
  actions.forEach(a=>{
    const jp=TOOLJP[a.tool]||a.tool||'—';
    const mode=a.kind==='dry'?'🔍 ドライ（実行せず）':'⚙ 実行';
    const args=a.input?JSON.stringify(a.input):'';
    const r=el('jrow');
    r.innerHTML=`<div class="jhead">🧠 判断: <b>${esc(jp)}</b> <span class="jmode">${mode}</span></div>`
      +`<div class="jline">動作: <code>${esc((a.tool||'')+args)}</code></div>`
      +`<div class="jline">結果: ${esc(a.result||'')}</div>`;
    box.appendChild(r);
  });
  log.appendChild(box); log.scrollTop=log.scrollHeight;
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
      body:JSON.stringify({text, dry_run:dry.checked, read_aloud:aloud.checked})});
    const data=await res.json();
    hideTyping();
    if(data.error){ bubble('bot','⚠ エラー: '+data.error); }
    else{ bubble('bot', data.reply); judge(data.actions); }  // 吹き出し→その下に判断ログ
    if(data.cost){
      const c=data.cost, el=document.getElementById('cost');
      el.textContent=`¥${c.turn}｜今日¥${c.today}｜今月¥${c.month}（ペース¥${c.pace}/月）`;
      el.style.color = c.pace>500 ? '#ff7a7a' : 'var(--muted)';  // 月500円ペース超で赤
    }
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
    pending_ephemeral = []  # 前ターンで開いた一時タブのURL（次ターン頭で閉じる・ADR-0021）

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
            aloud = bool(payload.get("read_aloud", False))
            if not text:
                self._send(200, json.dumps({"actions": [], "reply": "（何か入力してください）"}), "application/json")
                return
            # 前ターンで開いた一時タブを閉じる（ephemeralの後片付け・ADR-0021）
            if Handler.pending_ephemeral:
                tools.close_browser_tabs(Handler.pending_ephemeral)
                Handler.pending_ephemeral = []
            # 会話のリセット（記憶を消す）
            if text in ("リセット", "履歴クリア", "忘れて", "リセットして"):
                Handler.history = []
                self._send(200, json.dumps({"actions": [], "reply": "会話の記憶をリセットしました。"}, ensure_ascii=False), "application/json; charset=utf-8")
                return
            result = core.run_turn(self.client, self.persona, text, dry_run=dry, history=Handler.history)
            Handler.history = result.pop("history", Handler.history)  # 次ターンへ持ち越し（clientには返さない）
            Handler.pending_ephemeral = result.pop("ephemeral", [])  # 今ターンの一時タブは次ターンで閉じる
            u = result.pop("usage", None)
            result["cost"] = _log_cost(u) if u else None  # 実測コスト（今ターン/今日/今月/ペース）
            if aloud and result.get("reply"):  # 読み上げ（端で発声・非ブロッキング・ADR-0024）
                speak_mod.speak(result["reply"], block=False)
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
