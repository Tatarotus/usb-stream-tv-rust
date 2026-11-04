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
function confirm(s) { confirmCalls++; prompt = s; return !cancelled; }
async function pollVodStatus() {}
async function fetch(url, opts) {
    calls.push(url);
    requests.push({url, opts});
    if (url.startsWith('/api/vod/delete')) return {ok:true,json:async()=>({success:true})};
    if (url.startsWith('/api/mixed/status')) return {ok:true,json:async()=>statusAnswers.shift() || statusData};
    if (url.startsWith('/api/mixed/apply')) return {ok:true,json:async()=>applyAnswer};
    if (url.startsWith('/api/mixed/refresh')) return {ok:true,json:async()=>applyAnswer};
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
    assert.equal(JSON.parse(applyRequest.opts.body).confirm_interrupt, true);
    assert.equal(applyRequest.opts.headers['X-Auth-PIN'], 'pin');
    assert(calls.some(u=>u.startsWith('/api/mixed/confirm')));
    assert.equal(inventoryChecks, 1, 'full inventory must be confirmed');

    // An old terminal status must not be presented as the result of the new
    // request while the current inventory still differs from the tablet.
    calls=[]; banners=[]; inventoryChecks=0;
    applyAnswer={ok:true,available:true,state:'started'};
    statusData={available:true,state:'already_current',selected_in_active:true,apply_updated_ts:1};
    statusAnswers=[{available:true,state:'failed',reason:'old_failure',apply_updated_ts:1},
                   {available:true,state:'already_current',apply_updated_ts:2},
                   {available:true,state:'already_current',apply_updated_ts:2}];
    inventoryAnswers=[{available:true,confirmed:false},{available:true,confirmed:true}];
    await applyMixedGeneration('22222222','Outro',statusData,'pin');
    assert.equal(inventoryChecks,2, 'stale terminal must not end polling');
    assert(banners.some(s=>s.includes('Lista atualizada')));
    assert(!banners.some(s=>s.includes('Não foi possível atualizar')),
           'failure from previous apply must be ignored');

    // A blocked result must never request a second confirmation or retry.
    calls=[]; requests=[]; banners=[]; confirmCalls=0;
    applyAnswer={ok:true,available:true,state:'started'};
    statusAnswers=[{available:true,state:'blocked',reason:'active_vod_selected:x',apply_updated_ts:2}];
    await applyMixedGeneration('22222222','Outro',statusData,'pin');
    assert.equal(confirmCalls, 0);
    assert.equal(calls.filter(u=>u === '/api/mixed/apply').length, 1);
    assert(banners.some(s=>s.includes('Não foi possível autorizar')));

    calls=[]; requests=[]; banners=[]; confirmCalls=0;
    applyAnswer={ok:true,available:true,state:'blocked',reason:'telemetry_stale'};
    statusAnswers=[{available:true,state:'blocked',reason:'telemetry_stale',apply_updated_ts:2}];
    await refreshMixedInventory('Outro',statusData,'pin');
    const refreshRequest = requests.find(r=>r.url === '/api/mixed/refresh');
    assert.equal(JSON.parse(refreshRequest.opts.body).confirm_interrupt, true);
    assert.equal(refreshRequest.opts.headers['X-Auth-PIN'], 'pin');
    assert.equal(confirmCalls, 0);
    assert(banners.some(s=>s.includes('Não foi possível autorizar')));

    calls=[]; requests=[]; banners=[]; inventoryChecks=0;
    applyAnswer={ok:true,available:true,state:'started'};
    statusAnswers=[{available:true,state:'active',apply_updated_ts:1},
                   {available:true,state:'already_current',apply_updated_ts:2}];
    await refreshMixedInventory('Lista',statusData,'pin');
    assert.equal(inventoryChecks,1, 'stale success must not trigger confirmation');
    assert(banners.some(s=>s.includes('já estava atualizada')));
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
    render = html[html.index('        function renderLastEpisode()'):html.index('        async function loadLocalSavedMovies()', html.index('        function renderLastEpisode()'))]
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
    assert(label.textContent.includes('Selecionado anteriormente'));
    continueWatching=[]; renderLastEpisode(); assert(label.hidden);
}
run().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-'], input=script, text=True, check=True, timeout=10)
