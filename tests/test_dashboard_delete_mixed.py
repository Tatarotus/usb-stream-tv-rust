"""Exercise delete and update decisions with the actual dashboard JS."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_delete_defers_and_existing_episode_still_checks_full_inventory():
    html = Path('dashboard.html').read_text()
    start = html.index('        // Sonda o modo misto com três resultados.')
    end = html.index('        // Voltar do Cinema', start)
    functions = html[start:end]
    script = r'''
const assert = require('assert');
let vodTasks, banners, calls, prompt, cancelled, statusData, applyAnswer, confirmAnswer;
let inventoryAnswers, inventoryChecks, statusAnswers, requests, confirmCalls;
function triggerHaptic() {}
function escapeHtml(s) { return s; }
function getAuthPin() { return 'pin'; }
function showActionBanner(s) { banners.push(s); }
// Helper de PÁGINA (definido fora do recorte): o recorte o consome.
function timeoutSignal(ms) { return {}; }
function confirm(s) { confirmCalls++; prompt = s; return !cancelled; }
async function pollVodStatus() {}
async function fetch(url, opts) {
    calls.push(url);
    requests.push({url, opts});
    if (url.startsWith('/api/vod/delete')) return {ok:true,json:async()=>({success:true})};
    if (url.startsWith('/api/mixed/status')) return {ok:true,json:async()=>statusAnswers.shift() || statusData};
    if (url.startsWith('/api/mixed/apply')) return {ok:true,json:async()=>applyAnswer};
    if (url.startsWith('/api/mixed/refresh')) return {ok:true,json:async()=>applyAnswer};
    // O clique agora devolve o controle e o laço GLOBAL acompanha a operação;
    // ele sonda /api/mixed/events. Sem resposta aqui o laço viraria erro de
    // rede e o teste mediria outra coisa.
    if (url.startsWith('/api/mixed/events')) {
        return {ok:true,json:async()=>({ok:true,available:true,cursor:1,events:[],
                                        operations:[],unresolved:[]})};
    }
    if (url.startsWith('/api/mixed/confirm?task_id=')) {
        if (url.endsWith('task_id=')) {
            inventoryChecks++;
            return {ok:true,json:async()=>inventoryAnswers.shift() || confirmAnswer};
        }
        return {ok:true,json:async()=>confirmAnswer};
    }
    throw Error('unexpected '+url);
}
''' + functions + r'''
async function run() {
    vodTasks = [{id:'11111111',title:'Episódio',status:'ready',storage:'gdrive'}];
    banners=[]; calls=[]; requests=[]; confirmCalls=0; cancelled=false;
    statusData={available:true,state:'active',selected_in_active:true,apply_updated_ts:1};
    statusAnswers=[];
    applyAnswer={ok:true,available:true,state:'started'};
    confirmAnswer={ok:true,available:true,confirmed:true};
    inventoryAnswers=[]; inventoryChecks=0;
    await deleteVodTask('11111111');
    assert(calls.includes('/api/vod/delete'));
    assert(!calls.some(u=>u.startsWith('/api/mixed/refresh') || u.startsWith('/api/mixed/apply')),
           'deletion must not reconnect');
    assert(banners.some(s=>s.includes('NA TV')));
    assert(prompt.includes('quando você tocar'));

    calls=[]; banners=[];
    const savedTimeout=global.setTimeout;
    global.setTimeout=(fn)=>{fn(); return 0;};
    statusAnswers=[{available:true,state:'already_current',apply_updated_ts:2}];
    await applyMixedGeneration('22222222','Outro',statusData,'pin');
    assert(calls.includes('/api/mixed/apply'),
           'even an episode already on TV must check inventory with publisher');
    const applyRequest = requests.find(r=>r.url === '/api/mixed/apply');
    const applyBody = JSON.parse(applyRequest.opts.body);
    assert.equal(applyBody.confirm_interrupt, true);
    assert.equal(applyRequest.opts.headers['X-Auth-PIN'], 'pin');
    // UI1: uma chave por clique, para que repetir devolva a MESMA operação.
    assert.ok(applyBody.idempotency_key, 'the click must carry an idempotency key');
    // UI2: o clique NÃO roda laço próprio. Ele pede, devolve o controle e o
    // laço GLOBAL acompanha a operação (fase, estágio e desfecho).
    assert.equal(inventoryChecks, 0,
                 'the click must not run its own confirmation loop');
    // O laço global e a rota /api/mixed/events são provados no harness
    // dedicado (test_dashboard_mixed_ops.py); este recorte cobre o CLIQUE:
    // ele pede, carrega a chave e devolve o controle sem esperar resultado.
    assert.ok(banners.some(s=>s.indexOf('iniciada') !== -1),
              'the click reports that the update STARTED, not that it finished');
    assert.ok(!banners.some(s=>s.includes('Lista atualizada')),
              'the click must never claim the update finished on its own');

    // Uma chave diferente por clique: dois cliques não podem virar uma
    // operação reaproveitada por acidente.
    calls=[]; banners=[];
    await applyMixedGeneration('22222222','Outro',statusData,'pin');
    const second = requests.filter(r=>r.url === '/api/mixed/apply').pop();
    assert.notEqual(JSON.parse(second.opts.body).idempotency_key,
                    applyBody.idempotency_key,
                    'each click needs its own key');
    assert(!banners.some(s=>s.includes('Não foi possível atualizar')),
           'failure from previous apply must be ignored');

    // Uma recusa do endpoint (nada iniciado) aparece e NÃO é repetida: o clique
    // não tem laço próprio para repetir nada.
    calls=[]; requests=[]; banners=[]; confirmCalls=0;
    applyAnswer={ok:false,available:true,state:'blocked',reason:'active_vod_selected:x'};
    await applyMixedGeneration('22222222','Outro',statusData,'pin');
    assert.equal(confirmCalls, 0);
    assert.equal(calls.filter(u=>u === '/api/mixed/apply').length, 1,
                 'a refused request must not be retried by the click');
    assert.ok(banners.some(s=>s.includes('Não foi possível iniciar')),
              'a refusal is shown and nothing is claimed about the TV');

    // O clique de refresh segue o MESMO contrato: um POST com chave, sem laço.
    calls=[]; requests=[]; banners=[]; confirmCalls=0; inventoryChecks=0;
    applyAnswer={ok:true,available:true,state:'started'};
    await refreshMixedInventory('Outro',statusData,'pin');
    const refreshRequests = requests.filter(r=>r.url === '/api/mixed/refresh');
    assert.equal(refreshRequests.length, 1,
                 'the refresh click must send exactly one request');
    const refreshBody = JSON.parse(refreshRequests[0].opts.body);
    assert.equal(refreshBody.confirm_interrupt, true);
    assert.equal(refreshRequests[0].opts.headers['X-Auth-PIN'], 'pin');
    assert.ok(refreshBody.idempotency_key, 'refresh needs its own key per click');
    assert.equal(confirmCalls, 0,
                 'the refresh click must not run its own confirmation loop');
    assert.equal(inventoryChecks, 0,
                 'the refresh click must not wait for inventory confirmation');
    global.setTimeout=savedTimeout;

    calls=[]; banners=[]; cancelled=true;
    await deleteVodTask('11111111');
    assert(!calls.includes('/api/vod/delete'));
}
run().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-'], input=script, text=True, check=True, timeout=10)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_manual_episode_checkpoint_and_banner():
    html = Path('dashboard.html').read_text()
    assert 'onclick="refreshTvList()"' in html
    checkpoint = re.search(r'<button id="btn-vod-modal-checkpoint"[^>]+>', html).group()
    assert 'btn-modal-checkpoint' in checkpoint and 'btn-modal-web' not in checkpoint
    standalone = re.search(r'@media \(display-mode: standalone\)[^{]*\{([^}]+)', html).group(1)
    assert 'btn-modal-checkpoint' not in standalone and 'btn-vod-modal-checkpoint' not in standalone
    # O recorte começa na política de veracidade (P1): `renderLastEpisode`
    # depende de PLAYBACK_SOURCES/episodeWhen definidos logo acima dele.
    _p1 = html.index('        // P1 (veracidade): a linha NÃO pode sugerir')
    render = html[_p1:html.index('        async function loadLocalSavedMovies()', _p1)]
    start = html.index('        async function markSelectedEpisode()')
    end = html.index('        function deleteSelectedVodModal()', start)
    script = r'''
const assert = require('assert');
let continueWatching=[], selectedVodTaskForModal={id:'1234abcd',history_episode:{}};
let request, banners=[], label={hidden:true,textContent:''};
const document={getElementById:()=>label};
function getAuthPin(){return 'pin';}
function showActionBanner(s){banners.push(s);}
async function fetch(url,opts){request={url,opts};return {ok:true,json:async()=>({success:true,continue_watching:[{series_title:'Série',episode_number:3,position_source:'manual'}]})};}
''' + render + html[start:end] + r'''
async function run(){
    await markSelectedEpisode();
    assert.equal(request.url,'/api/vod/history/checkpoint');
    assert.equal(request.opts.headers['X-Auth-PIN'],'pin');
    assert.equal(JSON.parse(request.opts.body).task_id,'1234abcd');
    assert(!label.hidden && label.textContent.includes('Marcado por você') && label.textContent.includes('Episódio 3'));
    continueWatching=[{series_title:'Série',episode_number:4,position_source:'selected'}];
    renderLastEpisode();
    // P1: seleção NÃO é reprodução. O rótulo diz o que se sabe, sem afirmar
    // que algo foi assistido.
    assert(label.textContent.includes('Reprodução não registrada'), label.textContent);
    assert(!/assistid/i.test(label.textContent), 'never claim "assistido" for a selection');
    continueWatching=[{series_title:'Série',episode_number:230,position_source:'inferred_stream'}];
    renderLastEpisode();
    assert(label.textContent.includes('Possível reprodução na TV') && label.textContent.includes('Episódio 230'));
    continueWatching=[]; renderLastEpisode(); assert(label.hidden);
}
run().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-'], input=script, text=True, check=True, timeout=10)
