/* 回測詳情頁 render：讀 #equityCurveData（JSON）畫權益曲線 line chart。
   純前端、無框架；找不到 canvas/資料/Chart 時靜默 return（優雅降級）。 */
(function () {
  var el = document.getElementById('equityCurveData');
  var canvas = document.getElementById('equityChart');
  if (!el || !canvas || typeof Chart === 'undefined') return;

  var rows;
  try { rows = JSON.parse(el.textContent); } catch (e) { return; }
  if (!rows || !rows.length) return;

  var labels = rows.map(function (r) { return r.date; });

  new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        { label: '總權益', data: rows.map(function (r) { return r.equity; }), borderColor: '#1e293b', backgroundColor: '#1e293b', tension: .1, pointRadius: 0 },
        { label: '現金', data: rows.map(function (r) { return r.cash; }), borderColor: '#60a5fa', backgroundColor: '#60a5fa', tension: .1, pointRadius: 0 },
        { label: '持倉市值', data: rows.map(function (r) { return r.position_value; }), borderColor: '#f87171', backgroundColor: '#f87171', tension: .1, pointRadius: 0 }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 12 } },
        y: { ticks: { callback: function (v) { return v.toLocaleString(); } } }
      },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 12 } } },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              return ctx.dataset.label + '：' + ctx.parsed.y.toLocaleString() + ' TWD';
            }
          }
        }
      }
    }
  });
})();
