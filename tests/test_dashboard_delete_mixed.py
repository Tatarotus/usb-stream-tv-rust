"""Exercise delete and update decisions with the actual dashboard JS."""
from pathlib import Path
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
let inventoryAnswers, inventoryChecks, statusAnswers;
function triggerHaptic() {}
function escapeHtml(s) { return s; }
function getAuthPin() { return 'pin'; }
function showActionBanner(s) { banners.push(s); }
function confirm(s) { prompt = s; return !cancelled; }
async function pollVodStatus() {}
async function fetch(url, opts) {
    calls.push(url);
    if (url.startsWith('/api/vod/delete')) return {ok:true,json:async()=>({success:true})};
    if (url.startsWith('/api/mixed/status')) return {ok:true,json:async()=>statusAnswers.shift() || statusData};
    if (url.startsWith('/api/mixed/apply')) return {ok:true,json:async()=>applyAnswer};
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
    banners=[]; calls=[]; cancelled=false;
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
    global.setTimeout=savedTimeout;
    assert.equal(inventoryChecks,2, 'stale terminal must not end polling');
    assert(banners.some(s=>s.includes('Lista atualizada')));
    assert(!banners.some(s=>s.includes('Não foi possível atualizar')),
           'failure from previous apply must be ignored');

    calls=[]; banners=[]; cancelled=true;
    await deleteVodTask('11111111');
    assert(!calls.includes('/api/vod/delete'));
}
run().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run(['node', '-'], input=script, text=True, check=True, timeout=10)
