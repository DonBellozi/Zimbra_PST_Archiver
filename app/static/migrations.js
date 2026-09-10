(() => {
    const $ = id => document.getElementById(id);
    const csrf = $('imap-import').dataset.csrf;
    let batch = crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const n = crypto.getRandomValues(new Uint8Array(1))[0] & 15;
        return (c === 'x' ? n : (n & 3) | 8).toString(16);
    });
    let rows = [], previewed = null;
    const names = {QUEUED:'В очереди', SCANNING:'Оценка ящика', DOWNLOADING:'Загрузка',
        BUILDING_TGZ:'Сборка TGZ', CONVERTING:'Создание PST', VERIFYING:'Проверка',
        COMPLETED:'Готово', EMPTY:'Пустой ящик', FAILED:'Ошибка', CANCELLED:'Отменено',
        WAITING_FOR_SPACE:'Ожидает места', WAITING_FOR_SOURCE:'Ожидает IMAP', WAITING_FOR_STORAGE:'Ожидает хранилище', EXPIRED:'Файлы удалены'};
    const active = new Set(['SCANNING','DOWNLOADING','BUILDING_TGZ','CONVERTING','VERIFYING']);
    $('imap-csv').onchange = () => { previewed = null; $('imap-submit').disabled = true; };
    async function upload(preview) {
        const file = $('imap-csv').files[0];
        if (!file || file.size > 2*1024*1024) throw Error('Выберите CSV до 2 МБ');
        const query = new URLSearchParams({preview: preview?'1':'0', host:$('imap-host').value,
            port:$('imap-port').value, tls:$('imap-tls').value, storage:$('imap-storage').value,
            tgz:$('imap-tgz').checked?'1':'0', pst:$('imap-pst').checked?'1':'0', batch});
        const response = await fetch('/api/migrations/import?' + query, {method:'POST',
            headers:{'Content-Type':'text/csv; charset=utf-8','X-CSRF-Token':csrf}, body:file});
        const result = await response.json();
        if (!response.ok) throw Error(result.detail || 'Не удалось загрузить CSV');
        return result;
    }
    $('imap-preview').onclick = async () => {
        try { const result = await upload(true); previewed = $('imap-csv').files[0];
            $('imap-import-result').textContent = `Проверено: ${result.count} ящиков. ${result.accounts.slice(0,5).join(', ')}`;
            $('imap-submit').disabled = false;
        } catch(e) { $('imap-import-result').textContent = e.message; }
    };
    $('imap-import').onsubmit = async event => {
        event.preventDefault();
        if (previewed !== $('imap-csv').files[0]) return;
        $('imap-submit').disabled = true;
        try { const result = await upload(false); $('imap-import-result').textContent = `Добавлено в очередь: ${result.count}`;
            $('imap-csv').value = ''; previewed = null; await refresh();
            // New batch token, without keeping any credentials in web storage.
            batch = ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
        } catch(e) { $('imap-import-result').textContent = e.message; $('imap-submit').disabled = false; }
    };
    function render() {
        const query = $('migration-search').value.toLowerCase();
        const filtered = rows.filter(j => (j.account+' '+j.target+' '+j.status).toLowerCase().includes(query));
        $('migration-summary').textContent = `Показано ${filtered.length} из ${rows.length}. Готово: ${rows.filter(j=>j.status==='COMPLETED').length}`;
        const body = $('migration-jobs'); body.replaceChildren();
        for (const job of filtered) {
            const row = document.createElement('tr');
            const cell = text => { const td=document.createElement('td'); td.textContent=text; row.append(td); return td; };
            cell(job.account + (job.target!==job.account?' → '+job.target:''));
            cell((names[job.status]||job.status) + '\n' + (job.error||job.stage));
            cell(`${job.downloaded} / ${job.total}\n${fmt(job.downloaded_bytes)} / ${fmt(job.total_bytes)}`);
            const files = cell('');
            for (const [kind, enabled] of [['tgz',job.want_tgz&&job.tgz_status==='READY'],['pst',job.pst_status==='READY'],['report',true]]) {
                if (!enabled) continue;
                const link=document.createElement('a'); link.className='button';
                link.href=`/migrations/${job.id}/download/${kind}`; link.textContent=kind==='report'?'Отчёт':kind.toUpperCase(); files.append(link);
            }
            const controls = cell('');
            for (const [action,label] of [['retry','Повторить'],['rescan','Дозагрузить'],['cancel','Отменить'],['forget-password','Забыть пароль'],['delete-files','Удалить файлы']]) {
                const button=document.createElement('button'); button.textContent=label;
                button.disabled = (active.has(job.status)&&action!=='cancel') || (action==='rescan'&&!job.has_secret);
                button.onclick=async()=>{
                    const prompts={'delete-files':'Удалить все исходники и экспорты этой задачи? История останется.',
                        'forget-password':'Удалить сохранённый пароль? Дозагрузка потребует нового CSV.',
                        rescan:'Создать новый полный экспорт? Не импортируйте его поверх прежнего без проверки дубликатов.'};
                    if (prompts[action]&&!confirm(prompts[action])) return;
                    try { const r=await fetch(`/api/migrations/${job.id}/${action}`,{method:'POST',headers:{'X-CSRF-Token':csrf}});
                        if (!r.ok) throw Error((await r.json()).detail); await refresh();
                    } catch(e) { alert(e.message); }
                }; controls.append(button);
            }
            body.append(row);
        }
    }
    async function refresh() {
        try { const response=await fetch('/api/migrations',{cache:'no-store'});
            if (!response.ok) throw Error('Список недоступен. Возможно, нужно войти заново.');
            rows=await response.json(); render();
        } catch(e) { $('migration-summary').textContent=e.message; }
    }
    $('migration-search').oninput=render;
    $('migration-refresh').onclick=refresh;
    refresh(); setInterval(refresh,5000);
})();
