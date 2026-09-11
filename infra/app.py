"""The CodeGantry stack: one DynamoDB table holding every ledger, and the
one IAM user every host writes it as.

The account and region come from the signed-in CLI at synth time, so
nothing identifying is written here. The table survives the stack: a
ledger is the record of the work, and a `cdk destroy` must not take it.
The user's key is stored in Secrets Manager rather than emitted as an
output, and each host fetches it once into its repository's credentials
file.
"""

import os

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

TABLE_NAME = "code-gantry-ledger"
USER_NAME = "code-gantry"
SECRET_NAME = "code-gantry/ledger-user"


class CodeGantryStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # One table for every repository's ledger. `pk` names the ledger;
        # `seq` orders its events, with the counter item at seq 0, so a
        # replay is one Query and an append is a conditional put.
        table = dynamodb.Table(
            self, "Ledger",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            deletion_protection=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        user = iam.User(self, "User", user_name=USER_NAME)
        table.grant(
            user,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:Query",
            "dynamodb:BatchGetItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:TransactGetItems",
            "dynamodb:TransactWriteItems",
            "dynamodb:ConditionCheckItem",
            "dynamodb:DescribeTable",
        )

        key = iam.AccessKey(self, "Key", user=user)
        secretsmanager.Secret(
            self, "KeySecret",
            secret_name=SECRET_NAME,
            description="Access key for the code-gantry IAM user; fetched once per host into the repository's .code_gantry/env",
            secret_object_value={
                "AWS_ACCESS_KEY_ID": cdk.SecretValue.unsafe_plain_text(key.access_key_id),
                "AWS_SECRET_ACCESS_KEY": key.secret_access_key,
            },
        )

        cdk.CfnOutput(self, "TableName", value=table.table_name)
        cdk.CfnOutput(self, "SecretName", value=SECRET_NAME)


app = cdk.App()
CodeGantryStack(
    app, "CodeGantry",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    ),
)
app.synth()
