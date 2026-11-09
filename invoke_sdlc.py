"""
SDLC Accelerator 起動クライアント
Kiroで確定した仕様を渡してAI-DLCを全自動実行

使い方:
    python invoke_sdlc.py --auto-start                 # 承認済みspecを実行（Agent Hooksから呼ばれる形）
    python invoke_sdlc.py --feature slug-tool          # specを明示指定して実行

流れ:
    dev-agentが実装・テスト → 成果物を projects/<feature>/ に取り込む → 人間がレビュー

起動条件:
    実行対象は「.aidlc-approved マーカーが置かれたspecディレクトリ」だけ
    （--feature で明示指定した場合はマーカー不要）。仕様mdを保存しただけでは起動しない。
    同じ仕様内容での二重起動は指紋（.kiro/.aidlc/<feature>.launched）で防ぐ。
    --spec-dir を省略すると .kiro/specs/ を見る。
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import boto3
from botocore.config import Config

REGION = "us-east-1"


MEMORY_ID = "sdlc-project-memory"

# harness_config.json の "コード0行" 定義は CreateHarness でデプロイすると
# harness/<id> 形式のARNを持つ Harness リソースになる（deploy_dev_agent.py 参照）。
# これは runtime/<id> 形式の通常の AgentRuntime とは別物で、
# invoke_agent_runtime では呼べず invoke_harness を使う必要がある
# （実際に呼ぶと「is managed by a harness」というValidationExceptionで判明した）。
#
# 別の Harness を使うときは環境変数 AIDLC_DEV_AGENT_ARN で上書きする。
# 環境変数が無いときに AWS 上で探す Harness の名前（deploy_dev_agent.py の HARNESS_NAME と同じ）。
# Kiro の Hook から起動されたときは環境変数を設定できないため、名前で探せる必要がある。
HARNESS_NAMES = {
    "dev-agent": "aidlc_dev_agent",
}

_harness_arn_cache = {}


def harness_arn(agent_name: str) -> str:
    """
    エージェントの Harness ARN を返す
    環境変数 AIDLC_<名前>_ARN があればそれを使い、無ければ AWS 上の Harness を
    名前で探す。見つからなければ、デプロイを促すエラーにする。
    """
    env_key = "AIDLC_" + agent_name.replace("-", "_").upper() + "_ARN"
    if os.environ.get(env_key):
        return os.environ[env_key]
    if agent_name in _harness_arn_cache:
        return _harness_arn_cache[agent_name]

    name = HARNESS_NAMES.get(agent_name)
    if name:
        control = boto3.client("bedrock-agentcore-control", region_name=REGION)
        for summary in control.list_harnesses().get("harnesses", []):
            if summary["harnessName"] == name:
                _harness_arn_cache[agent_name] = summary["arn"]
                return summary["arn"]

    raise RuntimeError(
        f"{agent_name} が見つかりません。{REGION} にデプロイするか（python deploy_dev_agent.py）、"
        f"環境変数 {env_key} に ARN を設定してください。"
    )


def runtime_session_id(base: str, phase: str) -> str:
    """
    runtimeSessionId を組み立てる
    APIの制約は33文字以上256文字以下。短いfeature名だと33文字を下回って
    ValidationExceptionになるため、足りない場合は指紋で埋める。
    """
    sid = f"{base}-{phase}"
    if len(sid) < 33:
        pad = hashlib.sha256(sid.encode("utf-8")).hexdigest()
        sid = f"{sid}-{pad}"
    return sid[:256]


def _consume_stream(stream) -> list:
    """
    invoke_harness の event stream を読み、進捗を表示しながら
    モデルの発言（messageStop で区切られた1回分の出力）ごとの本文のリストを返す

    エージェントはツールを呼ぶたびに発言が区切られるので、作業中のつぶやき
    （"Now writing tests." など）と最後の完了報告は別の発言になる。

    イベント種別（実機で確認済み）:
      contentBlockDelta.delta.text  … 本文の断片
      messageStop                   … 1回分の発言の終わり
      validationException / internalServerException / runtimeClientError
        … エラー。runtimeClientErrorはツール実行時のIAM権限不足などで発生する
    """
    messages = []
    chunks = []
    for event in stream:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            text = delta.get("text")
            if text:
                chunks.append(text)
                print(text, end="", flush=True)
        elif "validationException" in event:
            raise RuntimeError(f"validationException: {event['validationException']}")
        elif "internalServerException" in event:
            raise RuntimeError(f"internalServerException: {event['internalServerException']}")
        elif "runtimeClientError" in event:
            raise RuntimeError(f"runtimeClientError: {event['runtimeClientError']}")
        elif "messageStop" in event:
            reason = event["messageStop"].get("stopReason")
            if reason == "content_filtered":
                # 会話履歴に危険なコード例が多く含まれると、モデルの安全フィルターで
                # 出力が止まることがある（実機で確認）。黙って空文字を返さず止める。
                raise RuntimeError(
                    "dev-agentの出力がモデルの安全フィルターで止められました"
                    "（stopReason=content_filtered）。会話をやり直してください。"
                )
            if chunks:
                messages.append("".join(chunks))
                chunks = []
                print()  # ログで発言どうしがつながらないように改行する
    if chunks:
        messages.append("".join(chunks))
    print()
    return messages


def invoke_agent_messages(
    runtime_client,
    agent_name: str,
    session_id: str,
    prompt: str,
    actor_id: str
) -> list:
    """
    エージェントを1回呼び出し、出力を表示しながら発言ごとの本文のリストを返す

    harness_config.json はコード0行のHarness定義なので、呼び出しは
    invoke_agent_runtime ではなく invoke_harness を使う（実機確認済み）。
    """
    response = runtime_client.invoke_harness(
        harnessArn=harness_arn(agent_name),
        runtimeSessionId=session_id,
        runtimeUserId=actor_id,
        actorId=actor_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    return _consume_stream(response["stream"])


def invoke_agent(runtime_client, agent_name: str, session_id: str,
                 prompt: str, actor_id: str) -> str:
    """エージェントを1回呼び出し、全発言をつなげた本文を返す"""
    return "".join(invoke_agent_messages(
        runtime_client, agent_name, session_id, prompt, actor_id))


def build_report(messages: list) -> str:
    """
    AIDLC_REPORT.md に保存する報告を作る

    最後の発言（完了報告）だけを使い、作業中のつぶやきは入れない。
    ファイル一覧（MANIFEST）は取り込みのための機械向けの記述なので取り除く。
    """
    final = next((m for m in reversed(messages) if m.strip()), "")
    report = re.sub(
        re.escape(MANIFEST_START) + r".*?" + re.escape(MANIFEST_END),
        "", final, flags=re.DOTALL,
    )
    report = re.sub(r"\n{3,}", "\n\n", report).strip()
    return report + "\n" if report else ""


def build_dev_prompt(project_spec: dict) -> str:
    design_block = (
        f"【design.md】:\n{project_spec['design_summary']}\n"
        if project_spec.get("design_summary") else ""
    )
    tasks_block = (
        f"【tasks.md】:\n{project_spec['tasks_summary']}\n"
        if project_spec.get("tasks_summary") else ""
    )

    return f"""
Kiroが確定した以下の仕様を実装してください。
あなたはサンドボックス内で動作しており、このワークスペースのファイルを
直接読むことはできません。仕様は以下の本文がすべてです。
作業は短時間で終えてください。サンドボックスは30分ほどで失効し、失効すると
成果物を取り込めなくなります（実装後に同じサンドボックスから全ファイルを取り出すため）。

そのため次を守ってください:
- 仕様に書かれた機能とそのテストだけを作る。仕様にない機能・ツール・ドキュメントは作らない
- サンドボックスに無いツール（pytest、coverage、bandit、pip-audit など）は自作しない
- テストは標準ライブラリの unittest で書いて実行する
- カバレッジは標準ライブラリの trace モジュールで行カバレッジだけ確認する
- セキュリティは、外部入力の扱い・eval/exec・シェル実行などがないかをコードを読んで確認し、報告に書く

【プロジェクト】: {project_spec['project_name']}
【スプリント】: {project_spec['sprint_id']}
【仕様ディレクトリ（参考情報。直接読めない）】: {project_spec['spec_path']}

【requirements.md】:
{project_spec['requirements_summary']}

{design_block}{tasks_block}
design.mdのレイヤ構成とtasks.mdのタスク分解に従い、依存のないモジュールから
実装してください。設計の補完 → 実装 → テスト生成と実行 → セキュリティスキャン
と修正、をテスト失敗とCRITICAL/HIGH脆弱性がゼロになるまで繰り返してください。

完了時に「テスト結果とカバレッジ / 残存する脆弱性（MEDIUM以下） /
設計上の判断とその理由」を出力してください。コード全文はこの報告に含めないで
ください（後で1ファイルずつ取り出します）。

報告の最後に、作成したすべてのファイル（ソース・テスト・ツール・ドキュメント・
依存定義を含む）のパスを、プロジェクトルートからの相対パスで次の形式で列挙
してください。1行に1パスです。

{MANIFEST_START}
mypackage/example.py
tests/test_example.py
{MANIFEST_END}
"""


# ─────────────────────────────────────────
# 成果物の取り込み
#
# dev-agentはサンドボックス内で動き、このワークスペースに書き込めない。
# そこで実装完了後、同じ会話（同じruntimeSessionId）の続きとしてファイルの
# 全文を数件ずつ出力させ、こちらで projects/<feature>/ に書き込む。
#
# 1回の報告に全ファイルを入れると分量の都合で省略されることが実際にあった
# ため、ファイル一覧（manifest）を先に受け取り、バッチに分けて取り出す。
#
# dev-agentの出力は信用しない前提で扱う。パスに絶対パスや ".." が含まれて
# いれば拒否し、書き込み先は projects/<feature>/ の中に限定する。
# ─────────────────────────────────────────

PROJECTS_DIR = Path("projects")
EXPORT_BATCH_SIZE = 4

MANIFEST_START = "<<<MANIFEST>>>"
MANIFEST_END = "<<<END MANIFEST>>>"
FILE_START = "<<<FILE path="
FILE_END = "<<<END FILE>>>"
SANDBOX_EXPIRED = "<<<SANDBOX EXPIRED>>>"


class SandboxExpiredError(RuntimeError):
    """サンドボックスが失効していて、テスト済みのファイルを取り出せない"""


def fetch_files(runtime_client, session_id: str, actor_id: str, paths: list) -> dict:
    """指定ファイルの内容を dev-agent に出力させて返す。失効していたら例外にする"""
    output = invoke_agent(runtime_client, "dev-agent", session_id,
                          build_export_prompt(paths), actor_id)
    if SANDBOX_EXPIRED in output:
        # 記憶から書き直したコードはテスト済みのものと別物になる（実機で確認済み）。
        # 黙って書き込まず、取り込みを止める。
        raise SandboxExpiredError(
            "dev-agentのサンドボックスが失効していて、テスト済みのファイルを読めません。"
            "--force で実装からやり直してください。"
        )
    return parse_files(output)

_MANIFEST_RE = re.compile(
    re.escape(MANIFEST_START) + r"\s*\n(.*?)\n\s*" + re.escape(MANIFEST_END), re.S
)


def parse_manifest(text: str) -> list:
    """報告文から最後の manifest ブロックを取り出し、パスの一覧を返す"""
    blocks = _MANIFEST_RE.findall(text)
    if not blocks:
        return []
    paths = []
    for line in blocks[-1].splitlines():
        line = line.strip().strip("`")
        if line and not line.startswith("#") and line not in paths:
            paths.append(line)
    return paths


def parse_files(text: str) -> dict:
    """
    出力から {パス: 内容} を取り出す

    1行ずつ見て、開始行と終了行の間を内容とする。正規表現で一括抽出すると、
    空のファイル（開始行の直後に終了行）のときに次のファイルの開始行まで
    取り込んでしまった（実機で確認）。

    開始記号は行の途中にあってもよい。dev-agent は説明文と開始記号を改行なしで
    続けて出力することがある（例: "I'll read the file.<<<FILE path=a.py>>>"、実機で確認）。
    終了記号の後ろに説明文が続いていても、そこで終わりとみなす。
    """
    files = {}
    current_path = None
    buffer = []
    for line in text.splitlines():
        if current_path is None:
            start = line.find(FILE_START)
            if start == -1:
                continue
            header = line[start + len(FILE_START):]
            end = header.find(">>>")
            if end == -1:
                continue
            current_path = header[:end].strip()
            buffer = []
        elif line.strip().startswith(FILE_END):
            files[current_path] = "\n".join(buffer)
            current_path = None
        else:
            buffer.append(line)
    return files


def safe_relative_path(raw: str) -> PurePosixPath:
    """プロジェクト外を指すパスを拒否し、正規化した相対パスを返す"""
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"プロジェクト外を指すパスは書き込みません: {raw!r}")
    return path


def build_manifest_prompt() -> str:
    return f"""
作成したすべてのファイルのパスを、プロジェクトルートからの相対パスで次の形式で
列挙してください。1行に1パスです。それ以外の説明は不要です。

{MANIFEST_START}
（パス）
{MANIFEST_END}
"""


def build_export_prompt(paths: list) -> str:
    listing = "\n".join(paths)
    return f"""
次のファイルを、サンドボックスから読み取って全文を出力してください。

{listing}

ルール:
- 必ずサンドボックス上のファイルを読んで、その内容をそのまま出力すること。
  会話の記憶から書き直してはいけません（細部が変わり、テスト済みのコードではなくなるため）。
- サンドボックスが失効していてファイルを読めない場合は、何も書き直さず
  次の1行だけを出力してください:
{SANDBOX_EXPIRED}
- 1ファイルずつ、次の形式で囲んでください。内容は省略・要約しないこと。
{FILE_START}<相対パス>>>
<ファイルの内容>
{FILE_END}
- コードブロック（```）で囲まないでください。
- テストの再実行やプロジェクトの再構築は不要です。
"""


def export_project(runtime_client, session_id: str, actor_id: str,
                   feature: str, dev_summary: str, report: str = "") -> Path:
    """
    dev-agentの成果物を projects/<feature>/ に書き込み、そのパスを返す

    dev_summary はファイル一覧（MANIFEST）を読むための dev-agent の全出力、
    report は AIDLC_REPORT.md に保存する完了報告（build_report で整えたもの）
    """
    paths = parse_manifest(dev_summary)
    if not paths:
        print("報告にファイル一覧がないため、一覧の出力を依頼します。")
        paths = parse_manifest(
            invoke_agent(runtime_client, "dev-agent", session_id,
                         build_manifest_prompt(), actor_id)
        )
    if not paths:
        raise RuntimeError("dev-agentからファイル一覧を取得できませんでした")

    for raw in paths:
        safe_relative_path(raw)  # 取り出す前に危険なパスを弾く

    print(f"取り出すファイル: {len(paths)}件")
    collected = {}
    batches = [paths[i:i + EXPORT_BATCH_SIZE] for i in range(0, len(paths), EXPORT_BATCH_SIZE)]
    for number, batch in enumerate(batches, start=1):
        print(f"\n--- 取り出し {number}/{len(batches)}: {', '.join(batch)} ---")
        collected.update(fetch_files(runtime_client, session_id, actor_id, batch))

    # 取り出せなかったファイルは1件ずつもう一度依頼する
    for path in [p for p in paths if p not in collected]:
        print(f"\n--- 再取得: {path} ---")
        collected.update(fetch_files(runtime_client, session_id, actor_id, [path]))

    missing = [p for p in paths if p not in collected]
    if missing:
        raise RuntimeError(f"取り出せなかったファイルがあります: {missing}")

    # 一時ディレクトリに全部書けてから差し替える。既存の成果物はバックアップに退避する
    target = PROJECTS_DIR / feature
    staging = PROJECTS_DIR / f".{feature}.incoming"
    if staging.exists():
        shutil.rmtree(staging)
    root = staging.resolve()
    for raw in paths:
        dest = (staging / safe_relative_path(raw)).resolve()
        if root not in dest.parents:
            raise ValueError(f"プロジェクト外を指すパスは書き込みません: {raw!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = collected[raw]
        if content and not content.endswith("\n"):
            content += "\n"
        dest.write_text(content, encoding="utf-8")
    if report:  # --export-only のときは報告がない
        (staging / "AIDLC_REPORT.md").write_text(report, encoding="utf-8")

    if target.exists():
        backup = PROJECTS_DIR / f"{feature}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        target.rename(backup)
        print(f"\n既存の成果物を退避しました: {backup}")
    staging.rename(target)
    print(f"成果物を書き込みました: {target}（{len(paths)}ファイル"
          + (" + AIDLC_REPORT.md" if report else "") + "）")
    return target


def run_project_tests(project_dir: Path) -> int:
    """成果物のテストをローカルで実行し、終了コードを返す"""
    print(f"\n成果物のテストを実行します: {project_dir}")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=project_dir,
    )
    print("テスト: " + ("PASS" if result.returncode == 0 else f"FAIL (exit {result.returncode})"))
    return result.returncode


def run_ai_dlc_lifecycle(
    project_spec: dict,
    export_only: bool = False,
    verify: bool = False,
) -> None:
    """
    AI駆動開発ライフサイクルを実行する

    フェーズ順序はここで固定する。順序は仕様上決まっていて分岐しないため、
    LLMに順番を判断させる必要がない。各フェーズの中の判断だけをエージェントに任せる。

    Kiro仕様 → dev-agent（設計補完・実装・テスト・脆弱性修正）
             → 成果物を projects/<feature>/ に取り込む
             → 人間がレビュー

    export_only=True なら実装はせず、既存の会話から成果物の取り込みだけ行う。
    verify=True なら取り込んだ成果物のテストをローカルで実行する。
    """
    # InvokeHarness は bedrock-agentcore クライアントにある
    # （bedrock-agentcore-runtime というサービス名は存在しない）
    #
    # dev-agentはCode Interpreter内で長時間の無応答区間（依存パッケージの
    # インストール試行、自作スキャナの実行など）を挟むことがあり、boto3の
    # デフォルトのread_timeout（60秒）だとストリームの合間でReadTimeoutErrorに
    # なる（実機で発生を確認）。実行上限（executionLimits.timeoutSeconds、
    # dev-agentは7200秒）に合わせてクライアント側のタイムアウトも延ばす。
    runtime_client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=Config(read_timeout=7200, connect_timeout=60, retries={"max_attempts": 0}),
    )

    actor_id = project_spec.get("requester", "developer")
    # sprint_idだけだと同日に複数featureを回したとき衝突するのでfeature名を含める
    session_base = f"sdlc-{project_spec['sprint_id']}-{project_spec['project_name']}"

    dev_session = runtime_session_id(session_base, "dev")

    print("🚀 AI-DLC を開始します")
    print(f"   プロジェクト: {project_spec['project_name']}")
    print(f"   仕様: {project_spec['spec_path']}")
    print(f"   セッション: {dev_session}")
    print("   フェーズ: dev-agent（実装） → 取り込み\n")

    if export_only:
        dev_summary = ""
        report = ""
    else:
        print("=" * 60)
        print("[1] dev-agent: 設計補完・実装・テスト・脆弱性修正")
        print("=" * 60)
        dev_messages = invoke_agent_messages(
            runtime_client,
            agent_name="dev-agent",
            session_id=dev_session,
            prompt=build_dev_prompt(project_spec),
            actor_id=actor_id,
        )
        # ファイル一覧の読み取りには全文を使い、報告には最後の発言だけを使う
        dev_summary = "".join(dev_messages)
        report = build_report(dev_messages)

    print()
    print("=" * 60)
    print(f"[2] 成果物の取り込み: projects/{project_spec['project_name']}/")
    print("=" * 60)
    project_dir = export_project(
        runtime_client, dev_session, actor_id, project_spec["project_name"], dev_summary,
        report=report,
    )

    if verify:
        run_project_tests(project_dir)
    else:
        print("\n成果物の中身を確認してから、テストを実行してください:")
        # この環境には python コマンドが無いことがあるため、--verify と同じく
        # いま動いている Python（.venv の Python）のパスを表示する
        print(f"  cd {project_dir} && {sys.executable} -m pytest -q")

    print()
    print("=" * 60)
    print(f"成果物（{project_dir}）と報告（AIDLC_REPORT.md）をレビューしてください。")


# ─────────────────────────────────────────
# 仕様の読み込み
# ─────────────────────────────────────────

# 人間が「この仕様で着手してよい」と示すためのマーカーファイル。
# 仕様mdの保存では起動せず、このファイルが置かれたspecだけを実行対象にする。
APPROVAL_MARKER = ".aidlc-approved"

# 起動済みspecの指紋を記録するディレクトリ（同じ内容での二重起動を防ぐ）
STATE_DIR = Path(".kiro/.aidlc")

# --spec-dir を省略したときに見るKiro仕様のルート
DEFAULT_SPEC_DIR = ".kiro/specs/"


def spec_fingerprint(feature_dir: Path) -> str:
    """spec配下のmdの内容から指紋を作る。内容が変わらなければ再起動しない"""
    digest = hashlib.sha256()
    for path in sorted(feature_dir.glob("*.md")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _state_file(feature: str) -> Path:
    return STATE_DIR / f"{feature}.launched"


def already_launched(feature: str, fingerprint: str) -> bool:
    state = _state_file(feature)
    if not state.is_file():
        return False
    return state.read_text(encoding="utf-8").strip() == fingerprint


def record_launch(feature: str, fingerprint: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(feature).write_text(fingerprint, encoding="utf-8")


def claim_launch(feature: str, fingerprint: str, force: bool = False) -> bool:
    """
    二重起動の判定と記録をファイルロックの中で行う。
    作成と保存の Hook がほぼ同時に起動しても、True を返すのは1つだけ
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{feature}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not force and already_launched(feature, fingerprint):
            return False
        record_launch(feature, fingerprint)
        return True


def find_approved_specs(spec_dir: str) -> list:
    """
    .kiro/specs/<feature>/ のうち、承認マーカーと requirements.md が
    揃っているものだけを返す
    """
    root = Path(spec_dir)
    if not root.is_dir():
        return []

    approved = []
    for marker in sorted(root.glob(f"*/{APPROVAL_MARKER}")):
        feature_dir = marker.parent
        if (feature_dir / "requirements.md").is_file():
            approved.append(feature_dir)
    return approved


def build_project_spec(feature_dir: Path) -> dict:
    """
    .kiro/specs/<feature>/ の requirements.md / design.md / tasks.md を読み取り、
    project_spec を組み立てる

    dev-agentはAWS側のサンドボックス（Code Interpreter）で動き、このマシンの
    ワークスペースを直接読めない。そのため3ファイルの全文をプロンプトに
    文字列として埋め込む必要がある（requirements.mdだけでは設計とタスクの
    詳細が伝わらない）。
    """
    requirements = feature_dir / "requirements.md"
    if not requirements.is_file():
        raise FileNotFoundError(f"requirements.md が見つかりません: {feature_dir}")

    def _read_if_exists(name: str) -> str:
        path = feature_dir / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    return {
        "project_name": feature_dir.name,
        # 実行ごとに新しい会話にする。日付だけだと同じ日のやり直しが前回の会話に
        # 積み重なり、入力が膨らんだうえ安全フィルターで出力が止まった（実機で確認）
        "sprint_id": f"sprint-{datetime.now():%Y%m%d-%H%M%S}",
        "spec_path": str(feature_dir),
        "requirements_summary": requirements.read_text(encoding="utf-8"),
        "design_summary": _read_if_exists("design.md"),
        "tasks_summary": _read_if_exists("tasks.md"),
        "requester": "developer",
    }


def resolve_target(args) -> Path:
    """
    実行対象の feature ディレクトリを1つに決める。
    決められない場合は None を返す（呼び出し側で終了コードを決める）
    """
    root = Path(args.spec_dir)

    # --feature 指定は人間の明示的な指示なのでマーカー不要
    if args.feature:
        feature_dir = root / args.feature
        if not feature_dir.is_dir():
            print(f"specディレクトリが見つかりません: {feature_dir}")
            return None
        return feature_dir

    approved = find_approved_specs(args.spec_dir)

    if not approved:
        print(
            f"承認済みのspecがありません（{APPROVAL_MARKER} が置かれた"
            f"specディレクトリなし）。何もせず終了します。"
        )
        return None

    if len(approved) > 1:
        names = ", ".join(d.name for d in approved)
        print(f"承認済みのspecが複数あります: {names}")
        print("--feature <name> でどれを実行するか指定してください。")
        return None

    return approved[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-DLC フルサイクル起動")
    parser.add_argument(
        "--spec-dir",
        default=DEFAULT_SPEC_DIR,
        help=(
            f"Kiro仕様のルート（省略時: {DEFAULT_SPEC_DIR}）。"
            f"配下の各specに {APPROVAL_MARKER} があるものだけを実行対象にする"
        ),
    )
    parser.add_argument(
        "--feature",
        help="実行するspecディレクトリ名を明示指定（承認マーカー不要）",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="確認プロンプトを出さずに即実行する（Agent Hooks用）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="同じ内容で既に起動済みでも再実行する",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="実装はせず、既存の会話から成果物の取り込みだけを行う（取り込みに失敗したときの再実行用）",
    )
    parser.add_argument(
        "--sprint-id",
        help=(
            "会話を特定するスプリントID（既定: sprint-<実行日時>。実行ごとに新しい会話になる）。"
            "--export-only で過去の実行を指すときは、その実行の開始時に表示されたIDを渡す"
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "取り込んだ成果物のテストをローカルで実行する。"
            "AIが書いたコードを自分のマシンで実行することになるため、中身を確認してから使うこと"
        ),
    )
    args = parser.parse_args()

    feature_dir = resolve_target(args)
    if feature_dir is None:
        # 対象なしは正常終了。フックが保存ごとに空振りしても失敗扱いにしない
        return 0

    project_spec = build_project_spec(feature_dir)
    if args.sprint_id:
        project_spec["sprint_id"] = args.sprint_id

    # 取り込みだけなら実装は走らないので、二重起動の判定・記録は行わない
    fingerprint = spec_fingerprint(feature_dir)
    if not args.export_only:
        if not args.force and already_launched(feature_dir.name, fingerprint):
            print(
                f"{feature_dir.name} は同じ仕様内容で既に起動済みです"
                f"（fingerprint: {fingerprint}）。--force で再実行できます。"
            )
            return 0

    if not args.auto_start:
        answer = input(
            f"AI-DLCを開始します（{project_spec['project_name']}）。よろしいですか？ [y/N]: "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("中止しました。")
            return 0

    if not args.export_only:
        # 起動前に記録する。実行中の再保存や、作成・保存の Hook の同時起動で二重起動しないようにするため
        if not claim_launch(feature_dir.name, fingerprint, force=args.force):
            print(
                f"{feature_dir.name} は同じ仕様内容で既に起動済みです"
                f"（fingerprint: {fingerprint}）。--force で再実行できます。"
            )
            return 0
    run_ai_dlc_lifecycle(
        project_spec,
        export_only=args.export_only,
        verify=args.verify,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
