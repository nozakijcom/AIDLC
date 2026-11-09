"""
dev-agent 単体を AgentCore Harness にデプロイするスクリプト

最小構成の AI-DLC で使うエージェントは dev-agent だけなので、これだけをデプロイする。

やること:
  1. dev-agent 用のIAM実行ロールを作成する（無ければ）
  2. harness_config.json の dev-agent 定義で CreateHarness を呼ぶ（無ければ）
  3. 実行時に使うランタイムARNを表示する

実APIは boto3 1.43.66 / botocore[crt] 導入済みの環境で確認済み:
  CreateHarness の必須パラメータは harnessName と executionRoleArn の2つだけ。
  harness.arn が invoke_agent_runtime の agentRuntimeArn に対応する。

使い方:
    python deploy_dev_agent.py            # 作成（既にあれば再利用）
    python deploy_dev_agent.py --dry-run  # 何が作られるかだけ確認
    python deploy_dev_agent.py --delete   # 後片付け（Harnessとロールを削除）
"""
import argparse
import json
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_NAME = "aidlc_dev_agent"  # harnessNameは [a-zA-Z][a-zA-Z0-9_]{0,39} のみ許可（ハイフン不可）
ROLE_NAME = "AIDLCDevAgentRole"
CONFIG_PATH = "harness_config.json"


def load_dev_agent_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return cfg["agents"]["dev-agent"]


def account_id(sts_client) -> str:
    return sts_client.get_caller_identity()["Account"]


def trust_policy(account: str) -> dict:
    """
    既存のAgentCore実行ロール（AgentCoreResearchAgentRole）と同じ形にする。
    bedrock-agentcore と bedrock の両方からAssumeRoleされる。
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {
                "Service": ["bedrock-agentcore.amazonaws.com", "bedrock.amazonaws.com"]
            },
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": account}},
        }],
    }


def permissions_policy(account: str) -> dict:
    """
    dev-agent が使うのは Bedrockモデル呼び出しと Code Interpreter だけ。
    Gateway・CodeCommit・CodePipelineへの権限は与えない
    （そもそも tools に持っていないので呼べないが、IAM側でも最小化する）。
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockInvoke",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{account}:inference-profile/*",
                ],
            },
            {
                # Harnessの実行に必要な操作一式（Code Interpreter起動、
                # 会話履歴/Memoryへの読み書き=ListEvents等を含む）。
                # 個別アクション列挙は実際にAccessDeniedで漏れが出たため、
                # dev-agent専用ロールという前提でbedrock-agentcore:*にまとめる
                # （Gateway/CodeCommit/CodePipelineへの権限はそもそも別サービスなので
                #  ここには含まれない）。
                "Sid": "AgentCoreHarnessRuntime",
                "Effect": "Allow",
                "Action": "bedrock-agentcore:*",
                "Resource": "*",
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{REGION}:{account}:*",
            },
        ],
    }


def ensure_role(iam_client, account: str) -> str:
    """
    実行ロールが無ければ作成し、ARNを返す（あれば既存を再利用）。
    権限ポリシーは常に最新の permissions_policy() で上書きする
    （put_role_policy は既存ポリシーがあれば置き換える）。
    """
    try:
        role = iam_client.get_role(RoleName=ROLE_NAME)["Role"]
        print(f"[role] 既存を再利用: {role['Arn']}")
        iam_client.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName="AIDLCDevAgentPolicy",
            PolicyDocument=json.dumps(permissions_policy(account)),
        )
        return role["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise

    # IAMのDescriptionはASCII+Latin-1補助文字までしか許可されないため英語で書く
    # （日本語での説明はこのファイル冒頭のコメントを参照）
    role = iam_client.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(trust_policy(account)),
        Description="AI-DLC dev-agent execution role. Bedrock invoke + Code Interpreter only.",
    )["Role"]

    iam_client.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="AIDLCDevAgentPolicy",
        PolicyDocument=json.dumps(permissions_policy(account)),
    )

    print(f"[role] 作成: {role['Arn']}")
    # IAMロール作成直後はAssumeRoleが伝播するまで数秒かかることがある
    time.sleep(10)
    return role["Arn"]


def find_existing_harness(control_client) -> dict | None:
    """
    list_harnesses の結果には executionRoleArn 等が含まれないため、
    見つかった場合は get_harness で詳細を取り直す。
    """
    for summary in control_client.list_harnesses().get("harnesses", []):
        if summary["harnessName"] == HARNESS_NAME:
            return control_client.get_harness(harnessId=summary["harnessId"])["harness"]
    return None


def build_create_harness_kwargs(dev_agent_cfg: dict, role_arn: str) -> dict:
    """
    harness_config.json の dev-agent 定義を CreateHarness の入力形式に変換する。
    dev-agent の tools は agentCoreCodeInterpreter のみ（Gatewayは持たない）。
    """
    model_id = dev_agent_cfg["model"]["modelId"]

    return {
        "harnessName": HARNESS_NAME,
        "executionRoleArn": role_arn,
        "model": {
            "bedrockModelConfig": {
                "modelId": model_id,
                # 8192だと実装+テスト+スキャン出力の途中で
                # maxTokensReachedExceptionに達することを実機で確認したため、
                # 既存のAutonomousResearchAgent（16384）を参考に増やす。
                "maxTokens": 32000,
                "apiFormat": "converse_stream",
            }
        },
        "systemPrompt": [{"text": dev_agent_cfg["systemPrompt"]}],
        "tools": [{"type": "agentcore_code_interpreter"}],
        "maxIterations": dev_agent_cfg["executionLimits"]["maxIterations"],
        "timeoutSeconds": dev_agent_cfg["executionLimits"]["timeoutSeconds"],
    }


def deploy(dry_run: bool) -> None:
    sts = boto3.client("sts", region_name=REGION)
    iam = boto3.client("iam")
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)

    account = account_id(sts)
    dev_agent_cfg = load_dev_agent_config()

    existing = find_existing_harness(control)

    if existing:
        kwargs = build_create_harness_kwargs(dev_agent_cfg, role_arn=existing["executionRoleArn"])
        # CreateHarness用のkwargsからharnessName/executionRoleArnを除いてUpdateHarness用にする
        update_kwargs = {k: v for k, v in kwargs.items() if k not in ("harnessName",)}
        update_kwargs["harnessId"] = existing["harnessId"]

        if dry_run:
            print(f"[dry-run] 既存Harness {existing['harnessId']} を以下の内容で更新します:")
            print(json.dumps({**update_kwargs, "systemPrompt": "(省略)"}, indent=2, ensure_ascii=False))
            return

        control.update_harness(**update_kwargs)
        print(f"[harness] 更新: {existing['arn']}")
        print()
        print("実行時に使うARN:")
        print(f"  export AIDLC_DEV_AGENT_ARN={existing['arn']}")
        return

    kwargs = build_create_harness_kwargs(dev_agent_cfg, role_arn="<role-arn>")

    if dry_run:
        print("[dry-run] 以下の内容でHarnessを作成します（実行はしません）:")
        print(json.dumps({**kwargs, "systemPrompt": "(省略)"}, indent=2, ensure_ascii=False))
        return

    role_arn = ensure_role(iam, account)
    kwargs["executionRoleArn"] = role_arn

    harness = control.create_harness(**kwargs)["harness"]
    print(f"[harness] 作成: {harness['arn']} (status={harness['status']})")

    print()
    print("実行時に使うARN（このまま環境変数に設定してください）:")
    print(f"  export AIDLC_DEV_AGENT_ARN={harness['arn']}")


def delete() -> None:
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    iam = boto3.client("iam")

    existing = find_existing_harness(control)
    if existing:
        control.delete_harness(harnessId=existing["harnessId"])
        print(f"[harness] 削除: {existing['harnessId']}")
    else:
        print("[harness] 対象なし")

    try:
        iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="AIDLCDevAgentPolicy")
        iam.delete_role(RoleName=ROLE_NAME)
        print(f"[role] 削除: {ROLE_NAME}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        print("[role] 対象なし")


def main() -> None:
    parser = argparse.ArgumentParser(description="dev-agent 単体をAgentCore Harnessにデプロイする")
    parser.add_argument("--dry-run", action="store_true", help="作成内容を表示するだけで実行しない")
    parser.add_argument("--delete", action="store_true", help="HarnessとIAMロールを削除する")
    args = parser.parse_args()

    if args.delete:
        delete()
        return

    deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
