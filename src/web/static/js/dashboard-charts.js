/* 儀表板圖表 render：讀 #allocData（JSON）畫資產配置 doughnut。
   純前端、無框架；找不到 canvas/資料/Chart 時靜默 return（優雅降級）。 */
(function () {
  var el = document.getElementById('allocData');
  var canvas = document.getElementById('allocChart');
  if (!el || !canvas || typeof Chart === 'undefined') return;

  var rows;
  try { rows = JSON.parse(el.textContent); } catch (e) { return; }
  if (!rows || !rows.length) return;

  var labels = rows.map(function (r) { return r.label + (r.stale ? '（估算）' : ''); });
  var values = rows.map(function (r) { return r.value; });

  var CASH_COLOR = '#cbd5e1';
  var PALETTE = [
    '#93c5fd', // 柔和藍
    '#fca5a5', // 柔和紅/粉紅
    '#fde047', // 柔和黃
    '#86efac', // 柔和綠
    '#c084fc', // 柔和紫
    '#fdba74', // 柔和橘
    '#67e8f9', // 柔和青
    '#f472b6', // 柔和粉
    '#94a3b8'  // 藍灰
  ];
  var colors = rows.map(function (r, i) {
    return r.kind === 'cash' ? CASH_COLOR : PALETTE[i % PALETTE.length];
  });

  new Chart(canvas.getContext('2d'), {
    type: 'doughnut',
    data: { labels: labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 1.5, borderColor: '#fff' }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '58%',
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 12 } } },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var v = ctx.parsed || 0;
              var total = ctx.dataset.data.reduce(function (a, b) { return a + b; }, 0);
              var pct = total ? (v / total * 100) : 0;
              return ctx.label + '：' + v.toLocaleString() + ' TWD（' + pct.toFixed(1) + '%）';
            }
          }
        }
      }
    }
  });
})();
