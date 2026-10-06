/* 權益圖表 render：dashboard 資料畫「總權益／現金／持倉市值 line + 每日損益 bar」；
   回測詳情資料維持既有的總權益／現金／持倉市值三線圖。
   純前端、無框架；找不到 canvas/資料/Chart 時靜默 return（優雅降級）。 */
(function () {
  var el = document.getElementById('equityCurveData');
  var canvas = document.getElementById('equityChart');
  if (!el || !canvas || typeof Chart === 'undefined') return;

  var rows;
  try { rows = JSON.parse(el.textContent); } catch (e) { return; }
  if (!rows || !rows.length) return;

  var labels = rows.map(function (r) { return r.date; });
  var hasDailyPnl = Object.prototype.hasOwnProperty.call(rows[0], 'daily_pnl');
  var datasets;
  var scales = {
    x: { ticks: { maxTicksLimit: 12 } },
    y: {
      position: 'left',
      ticks: { callback: function (v) { return v.toLocaleString(); } }
    }
  };

  if (hasDailyPnl) {
    datasets = [
      {
        type: 'line', label: '總權益',
        data: rows.map(function (r) { return r.equity; }),
        borderColor: '#1e293b', backgroundColor: '#1e293b',
        tension: .1, pointRadius: 0, yAxisID: 'y', order: 1
      },
      {
        type: 'line', label: '現金',
        data: rows.map(function (r) { return r.cash; }),
        borderColor: '#60a5fa', backgroundColor: '#60a5fa',
        tension: .1, pointRadius: 0, yAxisID: 'y', order: 1
      },
      {
        type: 'line', label: '持倉市值',
        data: rows.map(function (r) { return r.position_value; }),
        borderColor: '#f87171', backgroundColor: '#f87171',
        tension: .1, pointRadius: 0, yAxisID: 'y', order: 1
      },
      {
        type: 'bar', label: '每日損益',
        data: rows.map(function (r) { return r.daily_pnl; }),
        backgroundColor: rows.map(function (r) {
          if (r.daily_pnl === null) return 'transparent';
          return r.daily_pnl >= 0 ? '#e53e3e' : '#38a169';
        }),
        borderColor: rows.map(function (r) {
          if (r.daily_pnl === null) return 'transparent';
          return r.daily_pnl >= 0 ? '#e53e3e' : '#38a169';
        }),
        borderWidth: 1, maxBarThickness: 18, yAxisID: 'yPnl', order: 2
      },
      {
        type: 'bar', label: '已實現損益',
        data: rows.map(function (r) { return r.realized_pnl === undefined ? null : r.realized_pnl; }),
        backgroundColor: 'rgba(0,0,0,0)',
        borderColor: rows.map(function (r) {
          if (r.realized_pnl === null || r.realized_pnl === undefined) return 'transparent';
          return r.realized_pnl >= 0 ? '#e53e3e' : '#38a169';
        }),
        borderWidth: 2, maxBarThickness: 18, yAxisID: 'yPnl', order: 2, hidden: true
      }
    ];
    scales.yPnl = {
      position: 'right',
      beginAtZero: true,
      ticks: {
        callback: function (v) {
          return (v > 0 ? '+' : '') + v.toLocaleString();
        }
      },
      grid: {
        drawOnChartArea: true,
        color: function (ctx) {
          return ctx.tick && ctx.tick.value === 0 ? '#94a3b8' : 'rgba(0,0,0,0)';
        }
      }
    };
  } else {
    datasets = [
      { label: '總權益', data: rows.map(function (r) { return r.equity; }), borderColor: '#1e293b', backgroundColor: '#1e293b', tension: .1, pointRadius: 0 },
      { label: '現金', data: rows.map(function (r) { return r.cash; }), borderColor: '#60a5fa', backgroundColor: '#60a5fa', tension: .1, pointRadius: 0 },
      { label: '持倉市值', data: rows.map(function (r) { return r.position_value; }), borderColor: '#f87171', backgroundColor: '#f87171', tension: .1, pointRadius: 0 }
    ];
  }

  new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: scales,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 12 } } },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var value = ctx.parsed.y;
              if (value === null) return ctx.dataset.label + '：—';
              var sign = ctx.dataset.yAxisID === 'yPnl' && value > 0 ? '+' : '';
              return ctx.dataset.label + '：' + sign + value.toLocaleString() + ' TWD';
            }
          }
        }
      }
    }
  });

})();
