import * as path from 'path';
import { Annotations, Duration, Stack, StackProps } from 'aws-cdk-lib/core';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecsp from 'aws-cdk-lib/aws-ecs-patterns';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';
import { Construct } from 'constructs';

// Repo root (where the Dockerfile lives) — one level up from this CDK app.
const REPO_ROOT = path.resolve(__dirname, '..', '..');

// API keys are read from the shell env at `cdk deploy` time and passed straight
// to the task definition (phase 1 — no Secrets Manager). Never hardcode real
// values here; export them in the environment that runs the deploy. The app
// reads these via os.getenv(), so the names must match src/ exactly.
const API_KEY_ENV_VARS = [
  'OPENAI_API_KEY',
  'ANTHROPIC_API_KEY',
  'GEMINI_API_KEY',
  'GMAIL_ADDRESS',
  'GMAIL_APP_PASSWORD',
] as const;

// The app refuses to start (src/security.py) on the shipped admin/admin account
// or without a real APP_SECRET, so a deploy without these would crash-loop and
// roll back — fail at synth instead. Same shell-env pass-through as the keys.
const REQUIRED_SECURITY_ENV_VARS = ['APP_SECRET', 'ADMIN_PASSWORD'] as const;
// Optional: ADMIN_USERNAME (default "admin"); SESSION_COOKIE_SECURE=1 once the
// ALB has an HTTPS listener (leave unset while it is plain HTTP).
const OPTIONAL_SECURITY_ENV_VARS = ['ADMIN_USERNAME', 'SESSION_COOKIE_SECURE'] as const;

/**
 * Agent Auto System — single always-on Fargate task behind an ALB.
 *
 * Deliberately one task (`desiredCount = 1`): the app keeps run state in-process
 * and writes a single SQLite file, so a second task would split-brain. All
 * generated data lives on the task's ephemeral disk and is wiped on redeploy —
 * an accepted phase-1 trade. See doc/aws-ecs-fargate-deployment.md.
 */
export class AgentAutoSystemStackStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    // 1. Network — public-only VPC, no NAT Gateway. The task gets a public IP
    //    purely for egress to the LLM APIs; inbound is locked to the ALB SG.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [{ name: 'public', subnetType: ec2.SubnetType.PUBLIC }],
    });

    // 2. Cluster — Container Insights on for CPU/mem/task health.
    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // 3. Image — built from the repo's `runtime` Dockerfile stage and pushed to
    //    ECR by CDK on deploy. Pinned to linux/amd64 so it runs on X86_64
    //    Fargate regardless of the build host (Apple Silicon would otherwise
    //    produce an arm64 image and fail with "exec format error").
    const image = ecs.ContainerImage.fromAsset(REPO_ROOT, {
      target: 'runtime',
      platform: Platform.LINUX_AMD64,
    });

    // 4. API keys from the deploy environment. Only inject keys that are
    //    actually set — an empty string would override the app's own lookup and
    //    masquerade as "configured but blank". Warn at synth about any missing
    //    ones so a forgotten `export` is visible, not silent.
    //    DATABASE_URL is intentionally left at the Dockerfile default → SQLite
    //    on the task's local disk.
    const environment: Record<string, string> = {};
    const missing: string[] = [];
    for (const name of API_KEY_ENV_VARS) {
      const value = process.env[name];
      if (value) {
        environment[name] = value;
      } else {
        missing.push(name);
      }
    }
    if (missing.length > 0) {
      Annotations.of(this).addWarning(
        `No value provided for ${missing.join(', ')} at synth time. ` +
          `Export them before \`cdk deploy\` (e.g. \`OPENAI_API_KEY=sk-... cdk deploy\`) ` +
          `if the app needs them.`,
      );
    }

    const missingRequired: string[] = [];
    for (const name of REQUIRED_SECURITY_ENV_VARS) {
      const value = process.env[name];
      if (value) environment[name] = value;
      else missingRequired.push(name);
    }
    for (const name of OPTIONAL_SECURITY_ENV_VARS) {
      const value = process.env[name];
      if (value) environment[name] = value;
    }
    if (missingRequired.length > 0) {
      Annotations.of(this).addError(
        `${missingRequired.join(', ')} must be exported before \`cdk deploy\` — the app ` +
          `refuses to start on its shipped defaults. Use a long random APP_SECRET and keep ` +
          `it stable across deploys (it also encrypts API keys stored via the Admin page).`,
      );
    }

    // 5. Fargate service behind an ALB — exactly ONE task. minHealthyPercent=0
    //    forces stop-then-start deploys so two tasks never share the in-process
    //    state / SQLite file. ~30–60s downtime on deploy is the correct trade.
    const service = new ecsp.ApplicationLoadBalancedFargateService(this, 'Service', {
      cluster,
      cpu: 512,
      memoryLimitMiB: 1024,
      ephemeralStorageGiB: 21, // local disk for data/ uploads/ reports/
      desiredCount: 1,
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      publicLoadBalancer: true,
      assignPublicIp: true, // egress to LLM APIs without a NAT Gateway
      taskSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      healthCheckGracePeriod: Duration.seconds(60), // matches the image start-period
      circuitBreaker: { rollback: true },
      taskImageOptions: {
        image,
        containerPort: 8000,
        environment,
        logDriver: ecs.LogDrivers.awsLogs({
          streamPrefix: 'agent-auto-system',
          logRetention: logs.RetentionDays.ONE_MONTH,
        }),
      },
    });

    // 6. ALB health check → the app's DB-aware /health endpoint.
    service.targetGroup.configureHealthCheck({
      path: '/health',
      interval: Duration.seconds(30),
      healthyThresholdCount: 2,
    });

    // 7. Two ALB tweaks for this single-task, SSE-streaming app:
    //    - shrink the 300s default connection-drain so stop-then-start deploys
    //      don't hang ~5 min waiting for the old task to deregister;
    //    - raise the 60s default idle timeout so long-lived, low-traffic SSE
    //      log streams (GET /api/runs/{id}/stream) aren't cut mid-run.
    service.targetGroup.setAttribute('deregistration_delay.timeout_seconds', '15');
    service.loadBalancer.setAttribute('idle_timeout.timeout_seconds', '300');
  }
}
