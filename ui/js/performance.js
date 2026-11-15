// Performance (analytics) page.

import { TYPE_META } from './catalog.js';
import { escHtml } from './helpers.js';

// ── Performance page ──────────────────────────────────────────────────────────

let _perfDays = 7;  // selectable trend window (7 | 14 | 30)

export async function loadPerformancePage(days) {
  if (days) _perfDays = days;
  document.getElementById('perf-content').innerHTML = '<div class="loading-state">Loading metrics…</div>';
  try {
    const resp = await fetch(`/api/stats?days=${_perfDays}`);
    if (!resp.ok) throw new Error('Failed to load stats');
    renderPerformancePage(await resp.json());
  } catch (err) {
    document.getElementById('perf-content').innerHTML = `<div class="loading-state" style="color:var(--red)">Error: ${escHtml(err.message)}</div>`;
  }
}

function renderPerformancePage(s) {
  const maxTrend = Math.max(...s.trend.map(d => d.total), 1);

  const trendRows = s.trend.map(d => {
    const sw = Math.round((d.success / maxTrend) * 100);
    const fw = Math.round((d.failed  / maxTrend) * 100);
    const cost = d.cost || 0;
    const costStr = cost > 0 ? (cost < 0.01 ? '$' + cost.toFixed(5) : '$' + cost.toFixed(3)) : '·';
    const sc = d.avg_score;
    const scStr = sc == null ? '·' : Math.round(sc);
    const scColor = sc == null ? 'var(--text-muted)' : sc >= 80 ? 'var(--green)' : sc >= 50 ? 'var(--text-muted)' : 'var(--red)';
    return `
      <div class="trend-row">
        <div class="trend-label">${escHtml(d.label)}</div>
        <div class="trend-bar-wrap">
          <div class="trend-bar-success" style="width:${sw}%"></div>
          <div class="trend-bar-failed"  style="width:${fw}%"></div>
        </div>
        <div class="trend-count">${d.total}</div>
        <div style="min-width:64px;text-align:right;font-family:ui-monospace,monospace;font-size:0.72rem;color:var(--yellow)" title="cost this day">${costStr}</div>
        <div style="min-width:34px;text-align:right;font-size:0.72rem;color:${scColor}" title="avg quality score this day">${scStr}</div>
      </div>`;
  }).join('');

  const winBtn = (n) => `<button class="perf-win-btn${_perfDays === n ? ' active' : ''}" onclick="loadPerformancePage(${n})" style="padding:0.15rem 0.6rem;margin-left:0.35rem;border:1px solid var(--border);border-radius:5px;background:${_perfDays === n ? 'var(--accent,#3b82f6)' : 'transparent'};color:${_perfDays === n ? '#fff' : 'var(--text-muted)'};cursor:pointer;font-size:0.75rem">${n}d</button>`;

  const typeRows = Object.entries(s.by_type).map(([type, data]) => {
    const total = data.total || 1;
    const sw = Math.round((data.success / total) * 100);
    const fw = Math.round((data.failed  / total) * 100);
    const meta = TYPE_META[type] || { chip: type.toUpperCase(), cls: 'chip-unknown' };
    return `
      <tr>
        <td><span class="type-chip ${meta.cls}">${meta.chip}</span> ${escHtml(type)}</td>
        <td style="text-align:right">${data.total}</td>
        <td style="text-align:right;color:var(--green)">${data.success}</td>
        <td style="text-align:right;color:var(--red)">${data.failed}</td>
        <td>
          <div class="mini-bar-wrap">
            <div class="mini-bar-s" style="width:${sw}%"></div>
            <div class="mini-bar-f" style="width:${fw}%"></div>
          </div>
        </td>
        <td style="text-align:right;color:${data.retried > 0 ? 'var(--red)' : 'var(--text-muted)'}">${data.retried > 0 ? data.retried : '—'}</td>
        <td style="color:var(--text-muted)">${data.avg_duration > 0 ? data.avg_duration + 's' : '—'}</td>
      </tr>`;
  }).join('');

  const successRateColor = s.success_rate >= 80 ? 'green' : s.success_rate >= 50 ? '' : 'red';

  // Eval trust & reliability — is the headline quality number actually trustworthy?
  const indepRate = s.eval_independent_rate;   // % of scored runs graded by an independent LLM judge
  const indepColor = indepRate == null ? '' : indepRate >= 80 ? 'green' : indepRate >= 50 ? '' : 'red';
  const indepStr = indepRate == null ? '—' : indepRate + '%';
  const heurCount = s.eval_heuristic || 0;
  const retryColor = s.retry_rate === 0 ? 'green' : s.retry_rate <= 20 ? '' : 'red';
  // Fallback rate — lower is better (fewer cross-model failovers)
  const fbColor = s.fallback_rate === 0 ? 'green' : s.fallback_rate <= 10 ? '' : 'red';
  // Validation pass rate — higher is better
  const vpr = s.validation_pass_rate;
  const vprColor = vpr == null ? '' : vpr >= 90 ? 'green' : vpr >= 70 ? '' : 'red';
  const vprStr = vpr == null ? '—' : vpr + '%';
  const p95 = s.p95_duration_secs;
  const p50 = s.p50_duration_secs;
  const trustSection = `
    <div class="perf-section">
      <div class="perf-section-title">Eval Trust &amp; Reliability</div>
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-label" title="Share of scored runs graded by an independent LLM judge, excluding self-graded and heuristic fallbacks. Low = the quality score is not trustworthy.">Independent Judge</div>
          <div class="stat-value ${indepColor}">${indepStr}</div>
          <div class="stat-sub">${s.eval_independent || 0} of ${s.eval_scored || 0} scored</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Runs whose quality score is a heuristic fallback (no LLM judge available). These inflate/deflate Avg Quality without a real judgment.">Heuristic Scores</div>
          <div class="stat-value ${heurCount > 0 ? 'orange' : 'green'}" style="font-size:1.4rem">${heurCount}</div>
          <div class="stat-sub">${s.eval_llm || 0} LLM-judged</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of all runs that needed at least one retry (validation failure or transient LLM error). High = flaky provider or weak prompts.">Retry Rate</div>
          <div class="stat-value ${retryColor}">${s.retry_rate || 0}%</div>
          <div class="stat-sub">${s.retried_runs || 0} runs · avg ${s.avg_retries || 0}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of runs where the requested model failed and a different model had to serve the result. High = unreliable primary model/provider.">Fallback Rate</div>
          <div class="stat-value ${fbColor}">${s.fallback_rate || 0}%</div>
          <div class="stat-sub">${s.fallback_runs || 0} runs failed over</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="Percentage of runs that passed the validator quality gate on the attempt that was recorded. Low = outputs frequently malformed.">Validation Pass</div>
          <div class="stat-value ${vprColor}">${vprStr}</div>
          <div class="stat-sub">${s.validation_fails || 0} failed of ${s.validated_runs || 0}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label" title="95th-percentile run duration — the slow tail that averages hide.">p95 Duration</div>
          <div class="stat-value" style="font-size:1.4rem">${p95 == null ? '—' : p95 + 's'}</div>
          <div class="stat-sub">p50 ${p50 == null ? '—' : p50 + 's'}</div>
        </div>
      </div>
    </div>`;

  const totalCostStr = s.total_cost_usd > 0
    ? (s.total_cost_usd < 0.01 ? s.total_cost_usd.toFixed(5) : s.total_cost_usd.toFixed(3))
    : '0';
  const totalTokStr = s.total_tokens > 0 ? s.total_tokens.toLocaleString() : '0';

  // Per-provider cards with per-model breakdown
  const byModel = s.by_model || {};
  const allModelToks = Object.values(byModel).flat().map(m => m.tokens_in + m.tokens_out);
  const maxModelTok  = Math.max(...allModelToks, 1);

  const providerCards = Object.entries(s.by_provider || {}).map(([prov, d]) => {
    const provLabel = prov === 'anthropic' ? 'Claude' : prov === 'openai' ? 'OpenAI' : prov === 'gemini' ? 'Gemini' : prov;
    const badgeCls  = `llm-${prov}`;
    const provTok   = d.tokens_in + d.tokens_out;
    const provCost  = d.cost_usd > 0 ? (d.cost_usd < 0.01 ? '$' + d.cost_usd.toFixed(5) : '$' + d.cost_usd.toFixed(3)) : '$0';
    const provTokStr = provTok > 999 ? (provTok / 1000).toFixed(1) + 'k' : provTok.toString();

    const models = byModel[prov] || [];
    const modelRows = models.map(m => {
      const tok  = m.tokens_in + m.tokens_out;
      const inW  = Math.round((m.tokens_in  / Math.max(tok, 1)) * 100);
      const outW = 100 - inW;
      const barW = Math.round((tok / maxModelTok) * 72);
      const sr   = m.runs > 0 ? Math.round((m.success / m.runs) * 100) : 0;
      const srCls = sr >= 80 ? 'srate-high' : sr >= 50 ? 'srate-mid' : 'srate-low';
      const costStr = m.cost_usd > 0 ? (m.cost_usd < 0.0001 ? '<$0.0001' : '$' + m.cost_usd.toFixed(4)) : '$0';
      const modelShort = m.model.split('/').pop();
      const durStr = m.avg_duration > 0 ? m.avg_duration + 's' : '—';
      const score = m.avg_eval_score;
      const scoreCls = score == null ? 'srate-mid' : score >= 80 ? 'srate-high' : score >= 50 ? 'srate-mid' : 'srate-low';
      const scoreStr = score == null ? '—' : Math.round(score);
      const conf = m.avg_eval_confidence;
      const confStr = conf == null ? '—' : conf.toFixed(2);
      // Share of scored runs judged by an independent LLM (vs heuristic fallback)
      const scored = m.scored || 0;
      const llmPct = scored > 0 ? Math.round((m.llm_judged / scored) * 100) : 0;
      const judgeCls = scored === 0 ? 'srate-mid' : llmPct >= 80 ? 'srate-high' : llmPct >= 50 ? 'srate-mid' : 'srate-low';
      const judgeStr = scored === 0 ? '—' : llmPct + '%';
      const judgeTitle = scored === 0 ? 'no scored runs' : `${m.llm_judged}/${scored} scored by LLM judge, rest heuristic`;
      const retStr = m.retried > 0 ? m.retried : '—';
      const cps = m.cost_per_success;
      const cpsStr = cps == null ? '—' : cps < 0.0001 ? '<$0.0001' : '$' + cps.toFixed(4);
      const fbBadge = m.fallback > 0
        ? ` <span title="${m.fallback} run(s) failed over to this model" style="color:var(--red);font-size:0.65rem">⤳${m.fallback}</span>`
        : '';
      return `<tr>
        <td><span class="model-name-mono">${escHtml(modelShort)}</span>${fbBadge}</td>
        <td style="text-align:right">${m.runs}</td>
        <td><span class="srate-pill ${srCls}">${sr}%</span></td>
        <td><span class="srate-pill ${scoreCls}" title="confidence ${confStr}">${scoreStr}</span></td>
        <td><span class="srate-pill ${judgeCls}" title="${judgeTitle}">${judgeStr}</span></td>
        <td style="text-align:right;color:${m.retried > 0 ? 'var(--red)' : 'var(--text-muted)'}" title="runs that needed a retry">${retStr}</td>
        <td>
          <div class="tok-bar-wrap" style="width:${barW}px" title="${m.tokens_in.toLocaleString()} in · ${m.tokens_out.toLocaleString()} out">
            <div class="tok-bar-in"  style="width:${inW}%"></div>
            <div class="tok-bar-out" style="width:${outW}%"></div>
          </div>
          <div style="font-size:0.65rem;color:var(--text-muted);margin-top:0.15rem">${tok.toLocaleString()}</div>
        </td>
        <td style="color:var(--yellow);font-family:ui-monospace,monospace;font-size:0.8rem">${costStr}</td>
        <td style="color:var(--text-muted);font-family:ui-monospace,monospace;font-size:0.75rem" title="cost per successful run">${cpsStr}</td>
        <td style="color:var(--text-muted)">${durStr}</td>
      </tr>`;
    }).join('');

    return `
    <div class="llm-pcard pcard-${prov}">
      <div class="llm-pcard-header">
        <span class="llm-badge ${badgeCls}" style="font-size:0.75rem;padding:0.1rem 0.5rem">${escHtml(provLabel)}</span>
        <span class="llm-pcard-name">${escHtml(provLabel)}</span>
        <div class="llm-pcard-stats">
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val blue">${d.runs}</div>
            <div class="llm-pcard-stat-lab">Runs</div>
          </div>
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val" style="color:var(--text-muted)">${provTokStr}</div>
            <div class="llm-pcard-stat-lab">Tokens</div>
          </div>
          <div class="llm-pcard-stat">
            <div class="llm-pcard-stat-val yellow">${provCost}</div>
            <div class="llm-pcard-stat-lab">Cost</div>
          </div>
        </div>
      </div>
      ${modelRows ? `<table class="llm-model-table">
        <thead><tr>
          <th>Model</th><th style="text-align:right">Runs</th><th>Success</th><th>Score</th>
          <th title="% of scored runs graded by an independent LLM judge (vs heuristic fallback)">LLM&nbsp;Judged</th>
          <th style="text-align:right" title="runs that needed at least one retry">Retries</th>
          <th>Tokens <span style="opacity:0.5;font-weight:400">■ in ■ out</span></th>
          <th>Cost</th><th title="cost per successful run">$/Success</th><th>Avg Dur</th>
        </tr></thead>
        <tbody>${modelRows}</tbody>
      </table>` : ''}
    </div>`;
  }).join('');

  document.getElementById('perf-content').innerHTML = `
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-label">Total Runs</div>
        <div class="stat-value blue">${s.total_runs}</div>
        <div class="stat-sub">${s.active} active</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Success Rate</div>
        <div class="stat-value ${successRateColor}">${s.success_rate}%</div>
        <div class="stat-sub">${s.success} succeeded</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Failed</div>
        <div class="stat-value ${s.failed > 0 ? 'red' : ''}">${s.failed}</div>
        <div class="stat-sub">of ${s.total_runs} total</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Duration</div>
        <div class="stat-value">${s.avg_duration_secs > 0 ? s.avg_duration_secs + 's' : '—'}</div>
        <div class="stat-sub">completed runs</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Quality</div>
        <div class="stat-value ${s.avg_eval_score == null ? '' : s.avg_eval_score >= 80 ? 'green' : s.avg_eval_score >= 50 ? '' : 'red'}">${s.avg_eval_score == null ? '—' : s.avg_eval_score + '/100'}</div>
        <div class="stat-sub">${s.avg_eval_confidence == null ? 'eval score' : 'confidence ' + s.avg_eval_confidence}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Tokens</div>
        <div class="stat-value orange" style="font-size:1.4rem">${totalTokStr}</div>
        <div class="stat-sub">${(s.total_tokens_in||0).toLocaleString()} in · ${(s.total_tokens_out||0).toLocaleString()} out</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Cost</div>
        <div class="stat-value yellow" style="font-size:1.4rem">$${totalCostStr}</div>
        <div class="stat-sub">USD (estimated)</div>
      </div>
    </div>

    ${trustSection}

    <div class="perf-section">
      <div class="perf-section-title" style="display:flex;align-items:center;justify-content:space-between">
        <span>Last ${s.trend_days || _perfDays} Days <span style="font-weight:400;color:var(--text-muted);font-size:0.75rem">· runs · cost · quality</span></span>
        <span>${winBtn(7)}${winBtn(14)}${winBtn(30)}</span>
      </div>
      <div class="trend-chart">${trendRows}</div>
    </div>

    ${typeRows ? `
    <div class="perf-section">
      <div class="perf-section-title">By Automation Type</div>
      <div class="perf-table-wrap">
        <table class="perf-table">
          <thead>
            <tr>
              <th>Type</th>
              <th style="text-align:right">Total</th>
              <th style="text-align:right">Success</th>
              <th style="text-align:right">Failed</th>
              <th>Rate</th>
              <th style="text-align:right" title="runs that needed at least one retry">Retries</th>
              <th>Avg Duration</th>
            </tr>
          </thead>
          <tbody>${typeRows}</tbody>
        </table>
      </div>
    </div>` : ''}

    ${providerCards ? `
    <div class="perf-section">
      <div class="perf-section-title">LLM Usage — by Provider &amp; Model</div>
      <div class="llm-provider-cards">${providerCards}</div>
    </div>` : ''}`;
}
