# AI-DLC（AI駆動開発ライフサイクル）
人間が Kiro で仕様を書いて承認すると、AWS 上の AI エージェント（dev-agent）が実装・テスト・セキュリティスキャンを行い、成果物がこのリポジトリの `projects/<機能名>/` に入る仕組みです。
人間の作業は「仕様を書く」「承認マーカーを置く」「成果物をレビューする」の3つです。

## 流れ

```
人間
 │ ① Kiro で仕様を書く（requirements.md / design.md / tasks.md）
 │ ② レビューして OK なら、Kiro のチャットで「<機能名> を承認して」と頼む
 ▼
Kiro が承認マーカー .kiro/specs/<機能名>/.aidlc-approved を作る
 ▼
Kiro Agent Hook（マーカーを検知）
 ▼
invoke_sdlc.py（普通の Python。LLM ではない）
 │  仕様3ファイルの全文をプロンプトに入れて dev-agent を呼ぶ
 ▼
dev-agent（AgentCore Harness / Claude Opus 5）
 │  Code Interpreter サンドボックスの中で
 │  実装 → テスト → セキュリティスキャン → 修正 を全部通るまで繰り返す
 │  最後に作ったファイルの一覧を返す
 ▼
invoke_sdlc.py が成果物を取り込む
 │  同じ会話の続きで、ファイルを数件ずつサンドボックスから読み出させ、
 │  projects/<機能名>/ に書き込む（完了報告は AIDLC_REPORT.md に保存）
 ▼
人間がレビューし、テストを実行する
```

dev-agent はサンドボックスで動くため、このリポジトリのファイルを直接読み書きできません。仕様はプロンプトに全文を入れて渡し、成果物は会話を通じて取り出しています。

## 現在の状態

| 部分 | 状態 |
| --- | --- |
| チャットで承認 → Hook → 実装 → 取り込み | ✅ slug-tool で Kiro から通して確認済み（約6分。作成・保存の Hook は同時に発火したが、起動は1回だけ） |
| 取り込んだ成果物のテスト | 未実行（`cd projects/slug-tool && ../../.venv/bin/python -m pytest -q`） |
| release-agent（PR 作成・デプロイ） | 最小構成には不要なので作っていない（下記「release-agent について」を参照） |

補足: ファイルの作成・保存で発火する Hook は、Kiro（エージェント）がファイルを作ったり保存したりしたときだけ動きます。人がエディタやターミナルで承認マーカーを作っても発火しません（テスト用の Hook で確認しました）。そのため承認は、チャットで Kiro に頼んでマーカーを作らせます。Hook の設定ファイルを変えたときは、新しいセッションを開くまで反映されません。
また Kiro は Hook のコマンドを約60秒で打ち切ります（設定の `timeout` を 3600 にしても、dev-agent の実行が60秒で止まりました）。そのため Hook は `invoke_sdlc.py` を `nohup … &` で裏で起動して、すぐに終わるようにしています。マーカーを作ると作成と保存の Hook が両方発火しますが、実際に起動するのは1つだけです（ファイルロックで二重起動を防いでいます）。

## 例: slug-tool

`projects/slug-tool/` が、この仕組みで作った成果物です。文字列を URL 用の識別子に変換する `slugify()` 関数で、仕様（`.kiro/specs/slug-tool/`）には9個の受入基準を「WHEN 〜 THEN 〜」の形で書きました。例:

> WHEN アクセント付きのラテン文字を含む
> THEN アクセントを外した英字にする（`"Café Crème"` → `"cafe-creme"`）

```python
>>> from slugkit import slugify
>>> slugify("Café Crème")
'cafe-creme'
>>> slugify("abc日本語def")
'abc-def'
```

承認マーカーを置いてから約8分で、実装とテスト71件が `projects/slug-tool/` に入りました。dev-agent の報告は `projects/slug-tool/AIDLC_REPORT.md` にあります。

### 向いている仕様・向かない仕様
境界は「サンドボックスの中で、入力と出力だけで正しさを確認できるか」です。

- 向いている: 外部サービスに繋がない関数やライブラリ（CSV バリデータ、JSON 差分、入力値の検証など）
- 向かない: 実 DB・外部 API への接続が本質の機能、画面の見た目、性能要件が主眼のもの

受入基準は判定できる形で書きます。「適切にエラー処理する」ではなく「text が空なら空リストを返す」のように書かないと、エージェントが完了を判定できません。

## ファイル構成

```
.
├── README.md
├── requirements.txt          依存（boto3）
├── harness_config.json       dev-agent の定義（deploy_dev_agent.py が読む）
├── deploy_dev_agent.py       dev-agent を AWS にデプロイする
├── invoke_sdlc.py            仕様を読んで dev-agent を呼び、成果物を取り込む
├── projects/
│   └── slug-tool/            成果物（機能ごとに1フォルダ）
├── docs/architecture.md      構成図と仕組みの図（Mermaid）
├── docs/architecture.html    構成図（縦向き。ブラウザで開く）
└── .kiro/
    ├── specs/<機能名>/       仕様3ファイル（+ 承認マーカー）
    ├── hooks/                Kiro の自動実行設定
    └── .aidlc/               二重起動防止の記録
```

成果物を機能ごとのフォルダに分けているのは、次の機能を作ったときに前の成果物やこの仕組み自体のファイルを上書きしないためです（以前、成果物の `requirements.txt` がこの仕組みの `requirements.txt` を上書きしたことがありました）。

`.kiro/hooks/`:

| ファイル | 発火条件 | 動作 |
| --- | --- | --- |
| `spec-to-sdlc-trigger.json` | 承認マーカーの保存 | `invoke_sdlc.py` を起動 |
| `spec-to-sdlc-trigger-on-create.json` | 承認マーカーの新規作成（Kiro が作ったとき） | 同上 |
| `test-on-save.json` | `projects/` 配下の `.py` の保存 | 各プロジェクトのテストを実行 |
| `security-pre-commit.json` | `git commit` を含むコマンド | 実行前に確認を求める |

## セットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest            # 成果物のテスト実行に使う
```

AWS の認証（SSO）が必要です。切れたら `aws login` で再認証してください。

dev-agent を AWS にデプロイします（初回のみ）。

```bash
python deploy_dev_agent.py --dry-run   # 作られる内容を確認
python deploy_dev_agent.py             # IAM ロールと Harness を作成（既にあれば更新）
```

`invoke_sdlc.py` は AWS 上の `aidlc_dev_agent` を名前で探して使います。別の Harness を使いたいときだけ、環境変数 `AIDLC_DEV_AGENT_ARN` に ARN を設定してください。

後片付けは `python deploy_dev_agent.py --delete` です。

dev-agent はバージニア北部（us-east-1）にデプロイされます（`deploy_dev_agent.py` と `invoke_sdlc.py` でリージョンを固定しています）。コンソールで確認するときは、リージョンを us-east-1 に切り替えて **Amazon Bedrock AgentCore** の Harness 一覧を開いてください。名前は `aidlc_dev_agent` です。

## 使い方

### 1. 仕様を書く
Kiro で `.kiro/specs/<機能名>/` に requirements.md / design.md / tasks.md を作ります。

### 2. 実行する
Kiro のチャットで「<機能名> を承認して」と頼みます。Kiro が承認マーカーを作り、Hook が `invoke_sdlc.py` を起動します。経過は `.kiro/.aidlc/hook.log` に記録されます。
`touch` などで人が承認マーカーを作っても Hook は発火しません。Hook を使わずに起動するときは、次のコマンドを直接実行します。

```bash
.venv/bin/python invoke_sdlc.py --feature <機能名> --auto-start
```

関数1つ程度の仕様なら、実装と取り込みで数分です。終わると `projects/<機能名>/` に成果物、`AIDLC_REPORT.md` に dev-agent の報告が入ります。同じ名前のフォルダが既にあれば `projects/<機能名>.bak-<日時>/` に退避します。

同じ仕様内容で一度起動すると、再度マーカーを置いても起動しません。仕様を書き換えるか `--force` を付けると再実行できます。

### 3. レビューしてテストする

```bash
cd projects/<機能名>
../../.venv/bin/python -m pytest -q
```

成果物は AI が書いたコードです。テストを実行すると自分のマシンでそのコードが動く（AWS の認証情報にも触れられる状態で）ので、先に中身を確認してください。確認を省いて自動で実行したい場合は `--verify` を付けると、取り込み後にテストまで実行します。

### オプション
| オプション | 意味 |
| --- | --- |
| `--feature <名前>` | 対象の仕様を指定（承認マーカー不要） |
| `--auto-start` | 確認プロンプトを出さない（Hook から呼ぶとき用） |
| `--force` | 同じ仕様内容でも再実行する |
| `--verify` | 取り込み後にテストを実行する |
| `--export-only` | 実装はせず、直前の実行から取り込みだけやり直す |
| `--sprint-id <ID>` | `--export-only` で過去の実行を指すとき（既定は `sprint-<実行日時>`） |

## 注意点

### 取り込みはサンドボックスが生きている間しかできない
dev-agent の成果物はサンドボックスの中にしかありません。サンドボックスはしばらくすると失効し、ファイルが消えます。
失効後に取り込みを頼むと、dev-agent は会話の記憶からコードを書き直します。実際に試したところ、書き直したコードは21ファイルすべてが元と異なり、テストも1件落ちました。テスト済みのコードとは別物になります。
そのため取り込みでは「サンドボックスのファイルを読んで出力すること、読めなければ書き直さず失効を報告すること」を指示し、失効の報告を受けたら何も書き込まずに止めます。この場合は `--force` で実装からやり直してください。

dev-agent がサンドボックスのタイムアウトを延ばす手段はありません（使えるセッション作成の機能に設定項目がない）。そのため、作業を短く終えるよう指示しています。仕様にない機能やツールを作らないこと、テストは標準ライブラリの `unittest` で書くこと、などです。この指示を入れる前は、関数1つの仕様でも dev-agent が品質ツールの自作に1時間以上かけ、途中でサンドボックスが失効しました。入れた後は、取り込みまで含めて10分以内に終わっています。

### 実行ごとに新しい会話にしている
会話は実行日時で区別しています（`sprint-<日時>`）。以前は日付だけで区別していたため、同じ日のやり直しが前の会話に積み重なりました。その結果、入力が11万トークンを超え、モデルの安全フィルター（`content_filtered`）で出力が止まりました。この場合は、空の出力を黙って受け取らずにエラーで止めます。
`--export-only` で過去の実行を指すときは、その実行の開始時に表示された「セッション」の `sprint-...` 部分を `--sprint-id` に渡してください。

### 実機で判明したこと
| 事項 | 内容 |
| --- | --- |
| 呼び出し API | Harness は `InvokeHarness` で呼ぶ（`InvokeAgentRuntime` は不可）。ARN は `harness/...` 形式 |
| Harness 名 | ハイフン不可 |
| IAM の Description | 日本語不可 |
| maxTokens | 8192 では途中で打ち切られた。32000 に設定 |
| 読み取りタイムアウト | boto3 既定の60秒では途切れる。7200秒に設定 |
| サンドボックスのネットワーク | 外部に出られず pytest などを入れられない。dev-agent は標準ライブラリで代替ツールを自作した |
| モデルのパラメータ | Opus 5 は `temperature` を渡すとエラー |
| 認証 | SSO セッションが長時間の実行中に切れることがある |

## release-agent について
最初の構想では、2体目の release-agent が品質評価・CodeCommit への PR 作成・CodePipeline でのデプロイを行う予定でした。最小構成では作っていません。理由は2つです。

- slug-tool のようなライブラリにはデプロイ先がなく、人間がレビューして取り込めば完結する
- 今の設計には、コードを CodeCommit に push する担い手がいない、パイプラインが PR のコードをテストしない、という穴がある