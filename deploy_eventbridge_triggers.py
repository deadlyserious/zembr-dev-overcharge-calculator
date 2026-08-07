"""Create/update the EventBridge schedules for the overcharge calculator Lambda.

Thin boto3 wrapper around the four rules documented in the README's Deploy
section. Idempotent: re-running updates existing rules/targets/payloads in
place rather than duplicating them.

Usage:
    AWS_REGION=eu-north-1 python3 deploy_eventbridge_triggers.py \
        --function-name zembr-dev-overcharge-calculator

Requires credentials with permission to manage EventBridge rules/targets and
the Lambda's resource-based policy (``events:PutRule``, ``events:PutTargets``,
``lambda:AddPermission``, ``lambda:GetFunction``).
"""

import argparse
import json

import boto3

RULE_PREFIX = "overcharge-calculator-"

RULES = [
    {
        "name": "weekly",
        "description": "Weekly full run + emails (Sunday snapshot)",
        "schedule": "cron(0 6 ? * SUN *)",
        "payload": None,
    },
    {
        "name": "month-end-hourly",
        "description": "Month-end last-N-working-days recalculation guard",
        "schedule": "cron(0 * 26-31 * ? *)",
        "payload": {"trigger_mode": "last_n_working_days", "days": 3, "send_email": False},
    },
    {
        "name": "month-start-daily",
        "description": "Month-start first-N-working-days recalculation guard",
        "schedule": "cron(0 6 1-5 * ? *)",
        "payload": {"trigger_mode": "first_n_working_days", "days": 3, "send_email": False},
    },
    {
        "name": "monthly-totals",
        "description": "Previous-month totals report email (no Scoro write)",
        "schedule": "cron(0 7 1 * ? *)",
        "payload": {"trigger_mode": "monthly_totals"},
    },
]


def deploy_rule(events, lambda_client, function_arn, function_name, rule):
    rule_name = RULE_PREFIX + rule["name"]

    events.put_rule(
        Name=rule_name,
        ScheduleExpression=rule["schedule"],
        State="ENABLED",
        Description=rule["description"],
    )

    target = {"Id": "lambda", "Arn": function_arn}
    if rule["payload"] is not None:
        target["Input"] = json.dumps(rule["payload"])
    events.put_targets(Rule=rule_name, Targets=[target])

    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=rule_name,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{events.meta.region_name}:{boto3.client('sts').get_caller_identity()['Account']}:rule/{rule_name}",
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass  # permission already granted from a previous deploy

    print(f"deployed {rule_name}: {rule['schedule']} -> {rule['payload']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function-name", default="zembr-dev-overcharge-calculator")
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    events = session.client("events")
    lambda_client = session.client("lambda")

    function_arn = lambda_client.get_function(FunctionName=args.function_name)["Configuration"]["FunctionArn"]

    for rule in RULES:
        deploy_rule(events, lambda_client, function_arn, args.function_name, rule)


if __name__ == "__main__":
    main()
