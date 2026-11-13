# agent-auto-system-stack (CDK)

AWS CDK (TypeScript) infrastructure for deploying **Agent Auto System** to ECS
Fargate. Implements the phase-1 design in
[`../doc/aws-ecs-fargate-deployment.md`](../doc/aws-ecs-fargate-deployment.md):

> One always-on Fargate task behind an ALB. No NAT Gateway, no EFS, no Secrets
> Manager. Generated data (SQLite / uploads / reports) lives on the task's
> ephemeral disk and is wiped on redeploy — an accepted phase-1 trade.

The whole stack is in [`lib/agent-auto-system-stack-stack.ts`](lib/agent-auto-system-stack-stack.ts).
It builds the image from the repo's `runtime` Dockerfile stage (forced to
`linux/amd64`) and pushes it to ECR automatically on deploy.

## Deploy

API keys are read from your shell at deploy time and passed straight to the task
definition — **never hardcode them and never commit real values**.

Requires the AWS CDK CLI **≥ 2.1128.1** (matches the `aws-cdk-lib` cloud-assembly
schema): `npm install -g aws-cdk@latest`, then `cdk --version` to confirm.

```bash
npm install
npm run build && npm test          # tsc + jest

# First time in a new account/region:
cdk bootstrap

# Deploy. APP_SECRET + ADMIN_PASSWORD are required (synth fails without them —
# the app refuses to start on its shipped defaults). Keep APP_SECRET stable across
# deploys. Add SESSION_COOKIE_SECURE=1 once the ALB serves HTTPS.
# Export only the API keys you actually use; the rest are omitted.
APP_SECRET=...               \
ADMIN_PASSWORD=...           \
OPENAI_API_KEY=sk-...        \
ANTHROPIC_API_KEY=sk-ant-... \
GEMINI_API_KEY=...           \
GMAIL_ADDRESS=...            \
GMAIL_APP_PASSWORD=...       \
  cdk deploy



# example
export APP_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"   # save it; reuse on every deploy
export ADMIN_PASSWORD=...
export OPENAI_API_KEY=sk-...
cdk deploy
```

The stack outputs the ALB DNS name; open `http://<that-name>/` to reach the app.

> **Redeploys cause ~30–60s downtime and wipe all generated data** — both are
> intentional (single SQLite writer + ephemeral disk). See the design doc §7–§8
> for the additive upgrade path (EFS / SSM / Postgres / S3).

## Useful commands

| Command | Description |
|---|---|
| `npm run build` | compile TypeScript |
| `npm test` | jest assertions against the synthesized template |
| `cdk synth` | emit the CloudFormation template |
| `cdk diff` | diff deployed stack vs. current code |
| `cdk deploy` | build image, push to ECR, deploy |
| `cdk destroy` | tear everything down |
