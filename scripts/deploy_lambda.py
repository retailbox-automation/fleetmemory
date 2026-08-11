"""Deploy the FleetMemory demo to AWS Lambda (container image + function URL).

Idempotent: creates or updates ECR repo, IAM role, function, URL, and the
5-minute warmer schedule. Run:
  set -a && source .env && set +a && .venv/bin/python scripts/deploy_lambda.py
"""

import base64
import json
import os
import subprocess
import time

import boto3

REGION = "us-east-1"
REPO = "fleetmemory"
FUNC = "fleetmemory-demo"
ROLE = "fleetmemory-lambda"

iam = boto3.client("iam")
ecr = boto3.client("ecr", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
events = boto3.client("scheduler", region_name=REGION)


def sh(cmd):
    print("+", cmd if isinstance(cmd, str) else " ".join(cmd))
    subprocess.run(cmd, shell=isinstance(cmd, str), check=True)


def ensure_repo() -> str:
    try:
        r = ecr.create_repository(repositoryName=REPO)["repository"]
    except ecr.exceptions.RepositoryAlreadyExistsException:
        r = ecr.describe_repositories(repositoryNames=[REPO])["repositories"][0]
    return r["repositoryUri"]


def push_image(uri: str) -> str:
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, pw = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    registry = auth["proxyEndpoint"]
    subprocess.run(["docker", "login", "-u", user, "--password-stdin", registry],
                   input=pw.encode(), check=True)
    tag = f"{uri}:latest"
    sh(["docker", "tag", "fleetmemory:latest", tag])
    sh(["docker", "push", tag])
    digest = ecr.describe_images(repositoryName=REPO, imageIds=[{"imageTag": "latest"}]
                                 )["imageDetails"][0]["imageDigest"]
    return f"{uri}@{digest}"


def ensure_role() -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        role = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))["Role"]
        time.sleep(8)  # IAM propagation
    except iam.exceptions.EntityAlreadyExistsException:
        role = iam.get_role(RoleName=ROLE)["Role"]
    iam.attach_role_policy(RoleName=ROLE,
                           PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE, PolicyName="fleetmemory-runtime",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                            {"Effect": "Allow", "Action": ["bedrock:InvokeModel",
                                                           "bedrock:InvokeModelWithResponseStream"],
                             "Resource": "*"},
                            {"Effect": "Allow", "Action": ["bedrock-agentcore:*"],
                             "Resource": "*"},
                        ]}))
    return role["Arn"]


def ensure_function(image: str, role_arn: str) -> None:
    url = os.environ["FLEETMEM_URL"]
    if "sslrootcert" not in url:
        url += "&sslrootcert=/app/certs/root.crt"
    env = {
        "FLEETMEM_URL": url,
        "AGENTCORE_MEMORY_ID": os.environ["AGENTCORE_MEMORY_ID"],
        "FLEETMEM_MODEL": os.environ.get("FLEETMEM_MODEL",
                                         "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        "HOME": "/tmp",
    }
    try:
        lam.create_function(
            FunctionName=FUNC, PackageType="Image", Code={"ImageUri": image},
            Role=role_arn, Architectures=["arm64"], MemorySize=1536, Timeout=120,
            Environment={"Variables": env},
        )
        print("function created")
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=FUNC, ImageUri=image)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNC)
        lam.update_function_configuration(FunctionName=FUNC,
                                          Environment={"Variables": env},
                                          MemorySize=1536, Timeout=120)
        print("function updated")
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNC)


def ensure_url() -> str:
    try:
        cfg = lam.create_function_url_config(FunctionName=FUNC, AuthType="NONE")
    except lam.exceptions.ResourceConflictException:
        cfg = lam.get_function_url_config(FunctionName=FUNC)
    # since Oct 2025 a public URL needs BOTH statements
    for sid, action, extra in [
        ("public-url", "lambda:InvokeFunctionUrl", {"FunctionUrlAuthType": "NONE"}),
        ("public-url-invoke", "lambda:InvokeFunction", {"InvokedViaFunctionUrl": True}),
    ]:
        try:
            lam.add_permission(FunctionName=FUNC, StatementId=sid, Action=action,
                               Principal="*", **extra)
        except lam.exceptions.ResourceConflictException:
            pass
    return cfg["FunctionUrl"]


def ensure_warmer(url: str) -> None:
    """Ping every 5 min so judges never hit a cold start."""
    role_name = "fleetmemory-scheduler"
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        r = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(trust))
        time.sleep(8)
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    fn_arn = lam.get_function(FunctionName=FUNC)["Configuration"]["FunctionArn"]
    iam.put_role_policy(RoleName=role_name, PolicyName="invoke",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                            {"Effect": "Allow", "Action": "lambda:InvokeFunction",
                             "Resource": fn_arn}]}))
    sched_role = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    target = {"Arn": fn_arn, "RoleArn": sched_role,
              "Input": json.dumps({"requestContext": {"http": {"method": "GET", "path": "/"}},
                                   "rawPath": "/", "headers": {}, "warmer": True})}
    kwargs = dict(Name="fleetmemory-warmer", ScheduleExpression="rate(5 minutes)",
                  FlexibleTimeWindow={"Mode": "OFF"}, Target=target)
    try:
        events.create_schedule(**kwargs)
    except events.exceptions.ConflictException:
        events.update_schedule(**kwargs)


def main():
    uri = ensure_repo()
    image = push_image(uri)
    role_arn = ensure_role()
    ensure_function(image, role_arn)
    url = ensure_url()
    ensure_warmer(url)
    print("\nDEMO URL:", url)


if __name__ == "__main__":
    main()
