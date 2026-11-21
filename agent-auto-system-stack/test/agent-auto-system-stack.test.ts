import * as cdk from 'aws-cdk-lib/core';
import { Annotations, Template, Match } from 'aws-cdk-lib/assertions';
import * as AgentAutoSystemStack from '../lib/agent-auto-system-stack-stack';

function synth() {
  const app = new cdk.App();
  const stack = new AgentAutoSystemStack.AgentAutoSystemStackStack(app, 'TestStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  return Template.fromStack(stack);
}

test('runs a single Fargate task behind a public ALB', () => {
  const template = synth();

  // Exactly one always-on task (single SQLite writer / in-process registry).
  template.hasResourceProperties('AWS::ECS::Service', {
    DesiredCount: 1,
    DeploymentConfiguration: Match.objectLike({
      MinimumHealthyPercent: 0,
      MaximumPercent: 100,
    }),
  });

  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
    Scheme: 'internet-facing',
  });
});

test('task sizing and ephemeral storage match the design', () => {
  const template = synth();

  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    Cpu: '512',
    Memory: '1024',
    EphemeralStorage: { SizeInGiB: 21 },
  });
});

test('ALB health check targets /health', () => {
  const template = synth();

  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    HealthCheckPath: '/health',
  });
});

test('tunes the ALB for fast deploys and SSE streaming', () => {
  const template = synth();

  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
    LoadBalancerAttributes: Match.arrayWith([
      { Key: 'idle_timeout.timeout_seconds', Value: '300' },
    ]),
  });
  template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
    TargetGroupAttributes: Match.arrayWith([
      { Key: 'deregistration_delay.timeout_seconds', Value: '15' },
    ]),
  });
});

test('injects API keys from the environment and omits unset ones', () => {
  const prev = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = 'sk-test';
  delete process.env.ANTHROPIC_API_KEY;
  try {
    const template = synth();
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([{ Name: 'OPENAI_API_KEY', Value: 'sk-test' }]),
        }),
      ]),
    });
    // An unset key must NOT be injected as an empty string.
    const td = template.findResources('AWS::ECS::TaskDefinition');
    const env = Object.values(td)[0].Properties.ContainerDefinitions[0].Environment ?? [];
    expect(env.find((e: any) => e.Name === 'ANTHROPIC_API_KEY')).toBeUndefined();
  } finally {
    if (prev === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = prev;
  }
});

test('passes APP_SECRET / ADMIN_PASSWORD through and errors at synth when missing', () => {
  const names = ['APP_SECRET', 'ADMIN_PASSWORD', 'SESSION_COOKIE_SECURE'] as const;
  const prev = names.map((n) => process.env[n]);
  const errors = (stack: cdk.Stack) =>
    Annotations.fromStack(stack).findError('*', Match.stringLikeRegexp('APP_SECRET'));
  const build = () => {
    const app = new cdk.App();
    return new AgentAutoSystemStack.AgentAutoSystemStackStack(app, 'TestStack', {
      env: { account: '123456789012', region: 'us-east-1' },
    });
  };
  try {
    names.forEach((n) => delete process.env[n]);
    // The app would refuse to start → surface it at synth, not as a crash-loop.
    expect(errors(build()).length).toBe(1);

    process.env.APP_SECRET = 'test-secret';
    process.env.ADMIN_PASSWORD = 'test-password';
    process.env.SESSION_COOKIE_SECURE = '1';
    const stack = build();
    expect(errors(stack).length).toBe(0);
    Template.fromStack(stack).hasResourceProperties('AWS::ECS::TaskDefinition', {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            { Name: 'APP_SECRET', Value: 'test-secret' },
            { Name: 'ADMIN_PASSWORD', Value: 'test-password' },
            { Name: 'SESSION_COOKIE_SECURE', Value: '1' },
          ]),
        }),
      ]),
    });
  } finally {
    names.forEach((n, i) => {
      if (prev[i] === undefined) delete process.env[n];
      else process.env[n] = prev[i];
    });
  }
});

test('no NAT Gateway and no Secrets Manager (phase-1 simplicity)', () => {
  const template = synth();

  template.resourceCountIs('AWS::EC2::NatGateway', 0);
  template.resourceCountIs('AWS::SecretsManager::Secret', 0);
});
