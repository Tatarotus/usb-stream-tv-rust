#!/usr/bin/env python3
"""Global operation polling and the persistent indicator (Gerações V2, fase 1).

Runs the dashboard's own JavaScript under node with stubs, the same way
``test_dashboard_delete_mixed.py`` does. What is proven here is the *contract* of
the indicator: one loop per page, a pending problem is never hidden by
dismissing, cadence follows the documented numbers, and a network failure
deserves backoff rather than a claim about the TV.
"""

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

BEGIN = "        // ==================== Operações de publicação (Gerações V2)"
END = "        document.addEventListener('visibilitychange'"


def _slice() -> str:
    html = (ROOT / "dashboard.html").read_text()
    start = html.index(BEGIN)
    end = html.index(END, start)
    return html[start:end]


HARNESS = r"""
const assert = require('assert');

let timeouts = [];
let fetched = [];
let responses = [];
const el = {
    style: {},
    innerHTML: '',
    // Registra o último toggle de forma legível, preservando o nome da classe.
    classList: {
        last: null,
        toggle(name, value) { this.last = { name: name, value: value }; },
    },
};

global.setTimeout = (fn, ms) => { timeouts.push(ms); return timeouts.length; };
global.clearTimeout = () => {};
global.escapeHtml = (s) => String(s);
global.AbortSignal = { timeout: () => ({}) };
global.document = {
    getElementById: () => el,
    addEventListener: () => {},
    hidden: false,
};
global.fetch = async (url) => {
    fetched.push(url);
    const r = responses.shift();
    if (!r) throw new Error('no canned response for ' + url);
    if (r.throws) throw new Error('network down');
    return { ok: r.ok !== false, json: async () => r.body };
};

const settle = () => new Promise((res) => setImmediate(res));

async function run() {
    // A resposta canônica precisa existir ANTES do trecho rodar: o próprio
    // trecho chama startMixedOpsPolling() no fim, e sem resposta a primeira
    // sondagem cairia no backoff de falha de rede.
    responses.push({ body: { ok: true, available: true, cursor: 7,
                             events: [], operations: [], unresolved: [] } });
__SLICE__

    // --- 1. o laço arranca sozinho e usa a rota de eventos com cursor --------
    await settle();
    assert.ok(fetched.length >= 1, 'polled at least once');
    assert.ok(fetched[0].startsWith('/api/mixed/events?cursor='),
              'uses the events route, got ' + fetched[0]);
    assert.ok(timeouts.length >= 1, 'scheduled the next cycle');
    assert.strictEqual(timeouts[timeouts.length - 1], 15000,
                       'idle cadence is 15s');

    // --- 2. um único laço por página -----------------------------------------
    const scheduledBefore = timeouts.length;
    startMixedOpsPolling();
    assert.strictEqual(timeouts.length, scheduledBefore,
                       'a second start must not add a second loop');

    // --- 3. cadência acelera quando há pendência -----------------------------
    responses.push({ body: { ok: true, available: true, cursor: 9, events: [],
                             operations: [{ operation_id: 'op1', state: 'needs_reconciliation',
                                            user_message: 'Conexao perdida apos a troca',
                                            stage: 'verifying' }],
                             unresolved: ['op1'] } });
    await pollMixedEvents();
    assert.strictEqual(timeouts[timeouts.length - 1], 2000,
                       'pending problem polls at 2s');
    assert.strictEqual(el.style.display, 'block', 'indicator visible');
    assert.ok(el.innerHTML.indexOf('Conexao perdida') !== -1,
              'shows the user message');
    assert.strictEqual(el.classList.last.value, true,
                       'marked as a problem');
    assert.strictEqual(el.classList.last.name, 'is-problem',
                       'problem styling applied');

    // --- 4. dispensar NÃO esconde uma pendência ------------------------------
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'block',
                       'dismiss must not hide an unresolved problem');

    // --- 5. resultado terminal aparece e é dispensável -----------------------
    responses.push({ body: { ok: true, available: true, cursor: 11, events: [],
                             operations: [{ operation_id: 'op2', state: 'succeeded',
                                            user_message: 'Lista atualizada',
                                            stage: null }],
                             unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.classList.last.value, false, 'a success is not a problem');
    assert.ok(el.innerHTML.indexOf('Lista atualizada') !== -1);
    dismissMixedOpsIndicator();
    assert.strictEqual(el.style.display, 'none',
                       'a finished result can be dismissed');

    // --- 6. falha de rede faz backoff e não afirma nada ----------------------
    responses.push({ throws: true });
    await pollMixedEvents();
    const backoff = timeouts[timeouts.length - 1];
    assert.ok(backoff >= 2000 && backoff <= 30000,
              'backoff bounded, got ' + backoff);
    assert.strictEqual(el.style.display, 'none',
                       'no invented state after a network failure');

    // --- 7. sem store a rota diz available:false e nada é exibido ------------
    responses.push({ body: { ok: true, available: false, cursor: 0,
                             events: [], operations: [], unresolved: [] } });
    await pollMixedEvents();
    assert.strictEqual(el.style.display, 'none',
                       'no store means nothing to show');

    console.log('OK');
}

run().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_mixed_ops_indicator_contracts():
    script = HARNESS.replace("__SLICE__", _slice())
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=30, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "OK" in proc.stdout
