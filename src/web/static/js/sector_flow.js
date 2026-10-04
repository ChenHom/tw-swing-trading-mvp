/* 族群資金頁籤：第一次切到頁籤才 fetch /api/sector-flow，之後只渲染一次。資料合約見 data/sector_flow/dashboard.json 的產生器。 */
(function () {
  var root = document.getElementById('tab-sector'), holder = document.getElementById('tab-holder');
  if (!root || !holder) return;
  var started = false, inited = false;
  function $(id) { return document.getElementById('sf-' + id); }
  function reduced() { return matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'; }
  function message(text) { ['msg', 'lhmsg'].forEach(function (id) { var m = $(id); m.textContent = text; m.hidden = !text; }); }

  window.sectorFlowLoad = function () {
    if (started) return;
    started = true;
    message('載入中…');
    fetch(root.getAttribute('data-api'), { headers: { Accept: 'application/json' } })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          if (!res.ok || !j.rows || !j.dates || !j.dates.length) throw new Error(j.error || ('HTTP ' + res.status));
          return j;
        });
      })
      .then(function (D) { message(''); $('panel').hidden = false; inited = true; init(D); })
      .catch(function (err) { started = inited; message('資料載入失敗：' + err.message + '。切換頁籤可重試。'); });
  };

  function init(D) {
    var byName = {}; D.rows.forEach(function (r) { byName[r.name] = r; });
  var N = D.windows[0], sel = byName['半導體業'] ? '半導體業' : D.rows[0].name, subSel = null, lhSel = null;
  function yi(v) { var x = v / 1e8; return (x > 0 ? '+' : '') + x.toFixed(Math.abs(x) >= 100 ? 0 : 1) + ' 億'; }
  function cls(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
  function md(d) { return d.slice(5).replace('-', '/'); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function nm(r) { return esc(r.name) + (r.broad ? '<span class="tag">大類</span>' : ''); }
  function sum(a) { return a.reduce(function (s, v) { return s + v; }, 0); }
  function sign(v) { return (v > 0) - (v < 0); }
  function win(a) { return a.slice(-N); }
  function n() { return Math.min(N, D.dates.length); }

  // Window-independent: today, 5-day sum, streak, and 5-day-sum direction flips over the last 5 days.
  var last = D.dates[D.dates.length - 1];
  D.rows.forEach(function (r) {
    var a = r.daily, s5 = a.map(function (_, i) { return sum(a.slice(Math.max(0, i - 4), i + 1)); });
    r.today = a[a.length - 1]; r.d5 = s5[s5.length - 1]; r.prev5 = s5.length > 1 ? s5[s5.length - 2] : 0;
    var s = sign(r.today), k = 0; for (var i = a.length - 1; i >= 0 && s && sign(a[i]) === s; i--) k++; r.streak = k * s;
    r.flips = []; for (var j = Math.max(1, a.length - 5); j < a.length; j++) if (sign(s5[j]) && sign(s5[j]) !== sign(s5[j - 1])) r.flips.push({ date: D.dates[j], dir: s5[j] > 0 ? 'in' : 'out' });
    var f = r.flips[r.flips.length - 1]; r.flip = f && f.date === last ? f.dir : null;
  });

  function spark(vals) {
    var w = Math.max(120, vals.length * 2), h = 28, m = Math.max.apply(null, vals.map(Math.abs)) || 1, bw = w / vals.length;
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-label="' + vals.length + ' 日走勢">' +
      '<line x1="0" x2="' + w + '" y1="' + h / 2 + '" y2="' + h / 2 + '" stroke="#cbd5e0" stroke-width="1"/>' +
      vals.map(function (v, i) { var bh = Math.abs(v) / m * (h / 2 - 1); return '<rect x="' + (i * bw + .25) + '" width="' + Math.max(bw - .5, .5) + '" y="' + (v > 0 ? h / 2 - bh : h / 2) + '" height="' + Math.max(bh, .5) + '" fill="' + (v > 0 ? '#e53e3e' : '#38a169') + '"/>'; }).join('') + '</svg>';
  }

  function chart(r) {
    var ds = win(D.dates), vals = win(r.daily), cum = [], c = 0;
    var w = 640, h = 220, pl = 52, pr = 48, pt = 12, pb = 26;
    vals.forEach(function (v) { c += v; cum.push(c); });
    var lo = Math.min(0, Math.min.apply(null, vals), Math.min.apply(null, cum)), hi = Math.max(0, Math.max.apply(null, vals), Math.max.apply(null, cum));
    var y = function (v) { return pt + (hi - v) / (hi - lo || 1) * (h - pt - pb); }, bw = (w - pl - pr) / vals.length, gap = bw > 8 ? 2 : .4;
    var s = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" role="img" aria-label="' + esc(r.name) + ' 每日淨額、累計與指數">';
    [hi, (hi + lo) / 2, lo, 0].forEach(function (t) { s += '<line x1="' + pl + '" x2="' + (w - pr) + '" y1="' + y(t) + '" y2="' + y(t) + '" stroke="' + (t === 0 ? '#94a3b8' : '#edf2f7') + '"/><text x="' + (pl - 6) + '" y="' + (y(t) + 4) + '" text-anchor="end" font-size="10" fill="#718096">' + (t / 1e8).toFixed(0) + '億</text>'; });
    // index lines: % change from the first shown day, on the right axis
    var pct = function (a) { var b = a && a[0]; return b ? a.map(function (x) { return x == null ? null : (x / b - 1) * 100; }) : null; };
    var ix = pct(r.idxv && win(r.idxv)), tx = pct(D.taiex && win(D.taiex)), all = [].concat(ix || [], tx || []).filter(function (x) { return x != null; });
    var plo = Math.min(0, Math.min.apply(null, all)), phi = Math.max(0, Math.max.apply(null, all)), pad = (phi - plo) * .1 || 1;
    var y2 = function (p) { return pt + (phi + pad - p) / (phi - plo + 2 * pad) * (h - pt - pb); };
    var fp = function (p) { return (p > 0 ? '+' : '') + p.toFixed(1) + '%'; };
    var line = function (a, color, dash) { return '<polyline fill="none" stroke="' + color + '" stroke-width="2"' + (dash ? ' stroke-dasharray="5 4"' : '') + ' points="' + a.map(function (p, i) { return p == null ? null : (pl + i * bw + bw / 2) + ',' + y2(p); }).filter(Boolean).join(' ') + '"/>'; };
    [phi, 0, plo].filter(function (t, i, a) { return t !== 0 || Math.min(Math.abs(y2(0) - y2(a[0])), Math.abs(y2(0) - y2(a[2]))) > 12; }).forEach(function (t) { s += '<text x="' + (w - pr + 6) + '" y="' + (y2(t) + 4) + '" font-size="10" fill="#718096">' + fp(t) + '</text>'; });
    vals.forEach(function (v, i) { s += '<rect x="' + (pl + i * bw + gap) + '" width="' + Math.max(bw - 2 * gap, .5) + '" y="' + Math.min(y(v), y(0)) + '" height="' + Math.max(Math.abs(y(v) - y(0)), .5) + '" fill="' + (v > 0 ? '#e53e3e' : '#38a169') + '" opacity=".85"><title>' + ds[i] + '：' + yi(v) + (ix && ix[i] != null ? '｜' + esc(r.idx) + ' ' + fp(ix[i]) : '') + (tx && tx[i] != null ? '｜加權指數 ' + fp(tx[i]) : '') + '</title></rect>'; });
    s += '<polyline fill="none" stroke="#2b6cb0" stroke-width="2" points="' + cum.map(function (v, i) { return (pl + i * bw + bw / 2) + ',' + y(v); }).join(' ') + '"/>';
    s += '<circle cx="' + (pl + (vals.length - .5) * bw) + '" cy="' + y(cum[cum.length - 1]) + '" r="3.5" fill="#2b6cb0"/>';
    if (tx) s += line(tx, '#4a5568', true);
    if (ix) s += line(ix, '#dd6b20', false);
    [0, Math.floor(vals.length / 2), vals.length - 1].forEach(function (i) { s += '<text x="' + (pl + i * bw + bw / 2) + '" y="' + (h - 8) + '" text-anchor="middle" font-size="10" fill="#718096">' + md(ds[i]) + '</text>'; });
    // one nowrap span per swatch + label, so a wrapped legend never strands a swatch from its text
    var key = function (style, label) { return '<span style="white-space:nowrap"><i style="' + style + '"></i> ' + label + '</span>'; };
    return s + '</svg><div class="sf-legend">' + key('background:#e53e3e', '每日流入') + key('background:#38a169', '每日流出') + key('background:#2b6cb0;height:3px', '累計（左軸）') +
      (ix ? key('background:#dd6b20;height:3px', esc(r.idx) + '（右軸）') : '') + key('background:#4a5568;height:3px', '加權指數（右軸，虛線）') + '</div>' +
      '<p class="hint" style="margin:.3rem 0 0">指數以區間第一天為 0% 的漲跌幅。' + (ix ? '類股指數只含上市股票，族群金額含上櫃。' : '此族群沒有對應的證交所類股指數，只疊加權指數。') + '</p>';
  }
  function symLink(sym) { // same markup as the site's symcell macro
    return '<a class="sym-link" href="https://tw.stock.yahoo.com/quote/' + encodeURIComponent(sym) + '" target="_blank" rel="noopener">' + esc(sym) +
      '<svg class="ext-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg></a>';
  }
  function stockTable(list, title, side) {
    return '<div style="min-width:0"><div class="card-label" style="margin-bottom:.3rem">' + title + '</div><div class="sf-scroll"><table><thead><tr><th>代號</th><th>名稱</th><th class="num">估算金額</th><th class="num">佔' + side + '</th><th class="num">外資(張)</th><th class="num">投信(張)</th></tr></thead><tbody>' +
      (list.length ? list.map(function (s) { return '<tr><td>' + symLink(s.sym) + '</td><td>' + esc(s.name) + '</td><td class="num ' + cls(s.amt) + '">' + (s.amt == null ? '—' : yi(s.amt)) + '</td><td class="num">' + s.share.toFixed(1) + '%</td><td class="num ' + cls(s.f) + '">' + Math.round(s.f / 1000).toLocaleString() + '</td><td class="num ' + cls(s.t) + '">' + Math.round(s.t / 1000).toLocaleString() + '</td></tr>'; }).join('') : '<tr><td colspan="6">無</td></tr>') + '</tbody></table></div></div>';
  }
  function show(name) {
    if (name !== sel) subSel = null; // 換族群就清掉子類選取；換區間 (render -> show(sel)) 則保留
    sel = name;
    var r = byName[name], st = ((D.stocks || {})[N] || {})[name] || { in: [], out: [], subs: [], members: 0 }, k = n();
    root.querySelectorAll('tr.pick').forEach(function (tr) { tr.classList.toggle('sel', tr.getAttribute('data-n') === name); });
    var sg = null; st.subs.forEach(function (g) { if (g.name === subSel && g.in && g.out) sg = g; });
    if (!sg) subSel = null; // 新區間沒有這個子類：回到大類
    var subs = st.subs.length ? '<div class="card-label" style="margin:.8rem 0 .3rem">子類小計（近 ' + k + ' 日）' + (st.subs.some(function (g) { return g.in && g.out; }) ? '，點選一列看該子類排名' : '') + '</div><div class="sf-scroll"><table><thead><tr><th>子類</th><th class="num">檔數</th><th class="num">估算金額</th></tr></thead><tbody>' +
      st.subs.map(function (g) { var ok = g.in && g.out; return '<tr' + (ok ? ' class="sf-sub' + (g === sg ? ' sel' : '') + '" tabindex="0" data-sub="' + esc(g.name) + '"' : '') + '><td>' + esc(g.name) + '</td><td class="num">' + g.n + '</td><td class="num ' + cls(g.amt) + '">' + yi(g.amt) + '</td></tr>'; }).join('') + '</tbody></table></div>' : '';
    $('detail').innerHTML = '<h3>族群細看：' + nm(r) + '</h3><p class="hint">' + st.members + ' 檔成分股。近 ' + k + ' 日累計 <b class="' + cls(r.dn) + '">' + yi(r.dn) + '</b>，近 5 日 <b class="' + cls(r.d5) + '">' + yi(r.d5) + '</b>。</p>' +
      chart(r) + subs + (sg ? '<div class="sf-crumb" style="margin-top:.8rem"><span class="card-label" role="button" tabindex="0" data-crumb="1">' + esc(name) + '</span> › <b>' + esc(sg.name) + '</b></div>' : '') +
      '<div class="sf-two" style="margin-top:' + (sg ? '.3rem' : '.8rem') + '">' + stockTable((sg || st).in, '近 ' + k + ' 日流入前 5 名', '流入') + stockTable((sg || st).out, '近 ' + k + ' 日流出前 5 名', '流出') + '</div>';
  }

  // 大戶持股（週）：週資料，在 #tab-holder，不隨族群資金的區間切換
  function lhTable(list, title) {
    return '<div style="min-width:0"><div class="card-label" style="margin-bottom:.3rem">' + title + '</div><div class="sf-scroll"><table><thead><tr><th>代號</th><th>名稱</th><th class="num">估算金額</th><th class="num">大戶張數變化</th><th class="num">持股比例變化</th></tr></thead><tbody>' +
      (list.length ? list.map(function (s) { var lots = Math.round(s.shares / 1000);
        return '<tr><td>' + symLink(s.sym) + '</td><td>' + esc(s.name) + '</td><td class="num ' + cls(s.amt) + '">' + yi(s.amt) + '</td><td class="num ' + cls(lots) + '">' + (lots > 0 ? '+' : '') + lots.toLocaleString() + '</td><td class="num ' + cls(s.pp) + '">' + (s.pp > 0 ? '+' : '') + s.pp.toFixed(2) + ' pp</td></tr>'; }).join('') : '<tr><td colspan="5">無</td></tr>') + '</tbody></table></div></div>';
  }
  function renderLh() {
    var lh = D.large_holder, el = $('lh');
    if (!lh) { el.hidden = true; return; }
    el.hidden = false;
    var body = $('lhbody');
    if (lh.status !== 'ok' || !lh.rows || !lh.rows.length) {
      var why = lh.status === 'schema_error' ? 'TDCC 快照無法讀取'
        : lh.reason === 'snapshots_not_consecutive_weeks' ? '最近兩期 TDCC 週資料相隔超過兩週（' + esc(lh.prior) + ' → ' + esc(lh.latest) + '），不計算變化'
        : lh.reason === 'latest_snapshot_too_old' ? '最新的 TDCC 週資料（' + esc(lh.latest) + '）太舊，不計算變化'
        : '大戶變化需要兩期 TDCC 週資料，目前 ' + (lh.snapshots || 0) + ' 期';
      body.innerHTML = '<p class="hint" style="margin:0">' + why + '</p>';
      return;
    }
    var cur = null; lh.rows.forEach(function (r) { if (r.name === lhSel) cur = r; });
    if (!cur) { cur = lh.rows[0]; lhSel = cur.name; }
    body.innerHTML = '<p class="hint">' + esc(lh.prior) + ' → ' + esc(lh.latest) + '｜TDCC 集保週資料，已累積 ' + lh.snapshots + ' 期。大戶＝單一集保帳戶持有 400 張以上（含法人、ETF、大股東），增資、減資也會讓股數跳動。金額為大戶股數變化 × 收盤價的估算。</p>' +
      '<div class="sf-scroll"><table><thead><tr><th>族群</th><th class="num">大戶估算金額</th><th class="num">增加檔數</th><th class="num">減少檔數</th></tr></thead><tbody>' +
      lh.rows.map(function (r) { return '<tr class="sf-lh-row' + (r === cur ? ' sel' : '') + '" tabindex="0" data-n="' + esc(r.name) + '"><td>' + nm(r) + '</td><td class="num ' + cls(r.amt) + '">' + yi(r.amt) + '</td><td class="num">' + r.up + '</td><td class="num">' + r.down + '</td></tr>'; }).join('') + '</tbody></table></div>' +
      (cur.missing > 0 ? '<p class="hint" style="margin:.4rem 0 0">' + cur.missing + ' 檔無收盤價，未計入金額</p>' : '') +
      '<div class="sf-two" style="margin-top:.8rem">' + lhTable(cur.in || [], esc(cur.name) + ' 大戶增加前 5 名') + lhTable(cur.out || [], esc(cur.name) + ' 大戶減少前 5 名') + '</div>';
  }

  function render() {
    var k = n(), ds = win(D.dates), dense = k > 30;
    D.rows.forEach(function (r) { r.dn = sum(win(r.daily)); r.fn = sum(win(r.f)); r.tn = sum(win(r.t)); r.dln = sum(win(r.dl)); });
    var rows = D.rows.slice().sort(function (a, b) { return b.dn - a.dn; });
    root.querySelectorAll('.sf-nlabel').forEach(function (el) { el.textContent = k; });
    $('range').innerHTML = '<span class="card-label">觀察區間</span>' + D.windows.map(function (w) { return '<button type="button" data-days="' + w + '" aria-pressed="' + (w === N) + '">近 ' + w + ' 日</button>'; }).join('') +
      (N > D.dates.length ? '<span class="hint">目前資料只有 ' + D.dates.length + ' 個交易日</span>' : '');

    var todayIn = D.rows.filter(function (r) { return r.flip === 'in'; }).length, todayOut = D.rows.filter(function (r) { return r.flip === 'out'; }).length;
    var st = D.status === 'ok' ? ['正常', '四個來源齊全'] : D.status === 'degraded' ? ['部分降級', '原因見頁尾「資料警告」'] : ['資料受阻', '原因見頁尾「資料警告」'];
    $('cards').innerHTML = [
      ['資料日期', last, '每平日 22:00 更新'],
      ['觀察區間', k + ' 個交易日', md(ds[0]) + ' – ' + md(last)],
      ['今日轉為流入', todayIn + ' 個族群', '近 5 日累計由負轉正', 'in'],
      ['今日轉為流出', todayOut + ' 個族群', '近 5 日累計由正轉負', 'out'],
      ['資料狀態', st[0], st[1]]
    ].map(function (c) {
      var value = c[3] ? '<div class="card-value" data-flip="' + c[3] + '" role="button" tabindex="0" aria-haspopup="dialog">' : '<div class="card-value">';
      return '<div class="card"><div class="card-label">' + c[0] + '</div>' + value + c[1] + '</div><div class="hint" style="margin:0">' + c[2] + '</div></div>';
    }).join('');

    var flipped = D.rows.filter(function (r) { return r.flips.length; });
    flipped.sort(function (a, b) { return b.flips[b.flips.length - 1].date.localeCompare(a.flips[a.flips.length - 1].date) || Math.abs(b.d5) - Math.abs(a.d5); });
    $('flips').innerHTML = '<thead><tr><th>族群</th><th>轉向</th><th>日期</th><th class="num">近 5 日</th><th class="num">今日</th><th>' + k + ' 日走勢</th></tr></thead><tbody>' +
      (flipped.length ? flipped.map(function (r) { var f = r.flips[r.flips.length - 1];
        return '<tr class="pick" tabindex="0" data-n="' + esc(r.name) + '"><td>' + nm(r) + '</td><td><span class="badge ' + f.dir + '">' + (f.dir === 'in' ? '轉為流入' : '轉為流出') + '</span></td><td>' + md(f.date) + '</td><td class="num ' + cls(r.d5) + '">' + yi(r.d5) + '</td><td class="num ' + cls(r.today) + '">' + yi(r.today) + '</td><td>' + spark(win(r.daily)) + '</td></tr>'; }).join('')
        : '<tr><td colspan="6">近 5 個交易日沒有族群轉向。</td></tr>') + '</tbody>';

    var mx = 0; D.rows.forEach(function (r) { win(r.daily).forEach(function (v) { mx = Math.max(mx, Math.abs(v)); }); });
    function heat(v) { var t = Math.sqrt(Math.abs(v) / mx); if (t < .04) return '#f7fafc';
      var a = v > 0 ? [254, 215, 215, 197, 48, 48] : [198, 246, 213, 47, 133, 90];
      function mix(i) { return Math.round(a[i] + (a[i + 3] - a[i]) * t); } return 'rgb(' + mix(0) + ',' + mix(1) + ',' + mix(2) + ')'; }
    $('scale').textContent = '（最深 ≈ ' + yi(mx).replace('+', '') + '）';
    var heatEl = $('heat'); heatEl.classList.toggle('dense', dense);
    heatEl.innerHTML = '<thead><tr><th>族群</th>' + ds.map(function (d, i) { return '<th class="d">' + (dense && (k - 1 - i) % 5 ? '' : md(d)) + '</th>'; }).join('') + '<th class="num">' + k + ' 日</th></tr></thead><tbody>' +
      rows.map(function (r) { var v = win(r.daily); return '<tr class="pick" tabindex="0" data-n="' + esc(r.name) + '"><td class="n">' + nm(r) + '</td>' + v.map(function (x, i) { return '<td class="h" style="background:' + heat(x) + '" title="' + esc(r.name) + ' ' + ds[i] + '：' + yi(x) + '"></td>'; }).join('') + '<td class="num ' + cls(r.dn) + '">' + yi(r.dn) + '</td></tr>'; }).join('') + '</tbody>';

    $('rank').innerHTML = '<thead><tr><th>族群</th><th class="num">今日</th><th class="num">近 5 日</th><th class="num">近 ' + k + ' 日</th><th class="num">連續</th><th class="num">外資 ' + k + ' 日</th><th class="num">投信 ' + k + ' 日</th><th class="num">自營商 ' + k + ' 日</th></tr></thead><tbody>' +
      rows.map(function (r) { var s = r.streak; return '<tr class="pick" tabindex="0" data-n="' + esc(r.name) + '"><td>' + nm(r) + '</td><td class="num ' + cls(r.today) + '">' + yi(r.today) + '</td><td class="num ' + cls(r.d5) + '">' + yi(r.d5) + '</td><td class="num ' + cls(r.dn) + '">' + yi(r.dn) + '</td><td class="num ' + cls(s) + '">' + (s ? (s > 0 ? '流入 ' : '流出 ') + Math.abs(s) + ' 天' : '-') + '</td><td class="num ' + cls(r.fn) + '">' + yi(r.fn) + '</td><td class="num ' + cls(r.tn) + '">' + yi(r.tn) + '</td><td class="num ' + cls(r.dln) + '">' + yi(r.dln) + '</td></tr>'; }).join('') + '</tbody>';

    show(sel);
  }

  var dlg = $('dlg');
  function openFlips(dir) {
    var list = D.rows.filter(function (r) { return r.flip === dir; }).sort(function (a, b) { return dir === 'in' ? b.d5 - a.d5 : a.d5 - b.d5; });
    $('dlgtitle').textContent = (dir === 'in' ? '今日轉為流入' : '今日轉為流出') + '：' + list.length + ' 個族群';
    $('dlghint').textContent = last + ' 的近 5 日累計淨額' + (dir === 'in' ? '由負轉正' : '由正轉負') + '。點選族群可看細看。';
    $('dlgtable').innerHTML = '<thead><tr><th>族群</th><th class="num">前一日近 5 日</th><th class="num">今日近 5 日</th><th class="num">今日</th><th>' + n() + ' 日走勢</th></tr></thead><tbody>' +
      (list.length ? list.map(function (r) { return '<tr class="pick" tabindex="0" data-n="' + esc(r.name) + '"><td>' + nm(r) + '</td><td class="num ' + cls(r.prev5) + '">' + yi(r.prev5) + '</td><td class="num ' + cls(r.d5) + '">' + yi(r.d5) + '</td><td class="num ' + cls(r.today) + '">' + yi(r.today) + '</td><td>' + spark(win(r.daily)) + '</td></tr>'; }).join('')
        : '<tr><td colspan="5">今天沒有族群' + (dir === 'in' ? '轉為流入' : '轉為流出') + '。</td></tr>') + '</tbody>';
    dlg.showModal();
  }

  root.addEventListener('click', function (e) {
    var b = e.target.closest('#sf-range button');
    if (b) { N = +b.getAttribute('data-days'); render(); root.querySelector('#sf-range [aria-pressed="true"]').focus(); return; }
    var card = e.target.closest('[data-flip]');
    if (card) { openFlips(card.getAttribute('data-flip')); return; }
    if (e.target === dlg) { // a click on the backdrop lands on the dialog itself, outside its box
      var box = dlg.getBoundingClientRect();
      if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) dlg.close();
      return;
    }
    var sr = e.target.closest('tr.sf-sub'), cr = e.target.closest('[data-crumb]'), keep = null;
    if (sr || cr) { // 只換前 5 名表，不捲動、不動 sel 的族群
      var want = sr ? sr.getAttribute('data-sub') : null;
      keep = subSel; subSel = sr && want !== subSel ? want : null; show(sel);
      var back = root.querySelector('tr.sf-sub.sel') || Array.prototype.filter.call(root.querySelectorAll('tr.sf-sub'), function (t) { return t.getAttribute('data-sub') === keep; })[0];
      if (back) back.focus({ preventScroll: true });
      return;
    }
    var tr = e.target.closest('tr.pick');
    if (tr) { if (dlg.open) dlg.close(); show(tr.getAttribute('data-n')); $('detail').scrollIntoView({ behavior: reduced(), block: 'start' }); }
  });
  root.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.matches('tr.pick, [data-flip], tr.sf-sub, [data-crumb]')) e.target.click(); });
  // 大戶持股在自己的頁籤
  holder.addEventListener('click', function (e) {
    var lr = e.target.closest('tr.sf-lh-row');
    if (!lr) return;
    lhSel = lr.getAttribute('data-n'); renderLh();
    var again = Array.prototype.filter.call(holder.querySelectorAll('tr.sf-lh-row'), function (t) { return t.getAttribute('data-n') === lhSel; })[0];
    if (again) again.focus({ preventScroll: true });
  });
  holder.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.matches('tr.sf-lh-row')) e.target.click(); });

    // 附註：產業分類快照日期、資料警告（預設收合）
    if (D.taxonomy_snapshot_date) $('taxo').textContent = '產業分類為 ' + D.taxonomy_snapshot_date + ' 的快照。';
    var ws = D.warnings || [];
    if (ws.length) {
      $('wcount').textContent = ws.length + ' 則';
      $('wlist').innerHTML = ws.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('');
      $('warnings').hidden = false;
    }
    // 回到頂端：站台原本沒有同功能元件
    var toTop = $('totop');
    addEventListener('scroll', function () { toTop.hidden = scrollY < innerHeight; }, { passive: true });
    toTop.addEventListener('click', function () { scrollTo({ top: 0, behavior: reduced() }); });
    renderLh();
    render();
  }
})();
