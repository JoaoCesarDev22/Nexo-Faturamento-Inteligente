/* ============================================================
   NEXO — Landing Page · JS de mockups Plotly
   ============================================================
   Renderiza 3 gráficos demonstrativos:
     #hero-chart            barra vertical compacta (no mockup do hero)
     #demo-chart-evolucao   linha multissérie (5 meses de compras x vendas)
     #demo-chart-top        barra horizontal (top produtos do período)

   Tema espelha o design system NEXO (dark + paleta Hélio):
     - fundo transparente
     - tipografia Inter
     - paleta: roxo #a855f7, verde #22c55e, âmbar #f59e0b, info #38bdf8
     - tooltips dark com borda roxa

   Dados são FICTÍCIOS (loja de tintas demonstrativa "Tintas Almeida").
   ============================================================ */

(function () {
  if (typeof Plotly === 'undefined') return;

  /* ---- Paleta NEXO (mesma do nexo.css) ---- */
  var NEXO = {
    purple:   '#a855f7',
    purpleD:  '#7c3aed',
    green:    '#22c55e',
    amber:    '#f59e0b',
    info:     '#38bdf8',
    text:     '#e8e9f0',
    textMut:  '#9aa0b7',
    textDim:  '#6a7088',
    grid:     'rgba(255,255,255,0.06)',
    surface:  '#181824'
  };

  /* ---- Layout base reaproveitado pelos 3 gráficos ---- */
  function baseLayout(extra) {
    return Object.assign({
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor:  'rgba(0,0,0,0)',
      font: { family: 'Inter, sans-serif', color: NEXO.textMut, size: 11.5 },
      margin: { l: 48, r: 16, t: 10, b: 36 },
      xaxis: { gridcolor: NEXO.grid, zerolinecolor: NEXO.grid, automargin: true, fixedrange: true },
      yaxis: { gridcolor: NEXO.grid, zerolinecolor: NEXO.grid, automargin: true, fixedrange: true },
      showlegend: false,
      hoverlabel: {
        bgcolor: NEXO.surface,
        bordercolor: NEXO.purple,
        font: { color: NEXO.text, family: 'Inter, sans-serif', size: 11.5 }
      }
    }, extra || {});
  }

  var baseConfig = {
    displayModeBar: false,
    responsive: true,
    scrollZoom: false,
    editable: false,
    staticPlot: false
  };

  /* ============================================================
     1) hero-chart — barras compactas dentro do mockup do hero
        (Compras vs Vendas do mês)
     ============================================================ */
  (function () {
    var el = document.getElementById('hero-chart');
    if (!el) return;
    var labels = ['Compras', 'Vendas'];
    var values = [156300, 142800];
    var cores  = [NEXO.amber, NEXO.info];

    Plotly.newPlot(el, [{
      type: 'bar',
      x: labels,
      y: values,
      marker: { color: cores, line: { color: 'rgba(255,255,255,0.10)', width: 1 } },
      width: 0.5,
      text: values.map(function (v) {
        return 'R$ ' + v.toLocaleString('pt-BR', { maximumFractionDigits: 0 });
      }),
      textposition: 'outside',
      textfont: { color: NEXO.text, family: 'Inter, sans-serif', size: 11 },
      hovertemplate: '<b>%{x}</b><br>R$ %{y:,.0f}<extra></extra>',
      cliponaxis: false
    }], baseLayout({
      height: 158,
      bargap: 0.5,
      margin: { l: 52, r: 14, t: 22, b: 28 },
      yaxis: { gridcolor: NEXO.grid, tickprefix: 'R$ ', tickformat: ',.0f', fixedrange: true, automargin: true }
    }), Object.assign({}, baseConfig, { staticPlot: true }));
  })();

  /* ============================================================
     2) demo-chart-evolucao — linha multissérie de 5 meses
        Compras (laranja) vs Faturamento (azul neon)
     ============================================================ */
  (function () {
    var el = document.getElementById('demo-chart-evolucao');
    if (!el) return;
    var meses    = ['Dez/25', 'Jan/26', 'Fev/26', 'Mar/26', 'Abr/26'];
    var compras  = [138400, 124900, 149600, 152300, 156300];
    var vendas   = [132100, 119800, 138200, 141600, 142800];

    Plotly.newPlot(el, [
      {
        type: 'scatter', mode: 'lines+markers',
        name: 'Compras',
        x: meses, y: compras,
        line: { color: NEXO.amber, width: 2.5, shape: 'spline', smoothing: 0.8 },
        marker: { color: NEXO.amber, size: 7, line: { color: '#0c0c14', width: 2 } },
        hovertemplate: '<b>%{x}</b><br>Compras: R$ %{y:,.0f}<extra></extra>'
      },
      {
        type: 'scatter', mode: 'lines+markers',
        name: 'Vendas',
        x: meses, y: vendas,
        line: { color: NEXO.info, width: 2.5, shape: 'spline', smoothing: 0.8 },
        marker: { color: NEXO.info, size: 7, line: { color: '#0c0c14', width: 2 } },
        hovertemplate: '<b>%{x}</b><br>Vendas: R$ %{y:,.0f}<extra></extra>'
      }
    ], baseLayout({
      height: 220,
      showlegend: true,
      legend: {
        orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center',
        font: { color: NEXO.textMut, size: 11 }
      },
      yaxis: { gridcolor: NEXO.grid, tickprefix: 'R$ ', tickformat: ',.0f', fixedrange: true, automargin: true },
      margin: { l: 60, r: 18, t: 14, b: 48 }
    }), Object.assign({}, baseConfig, { staticPlot: false }));
  })();

  /* ============================================================
     3) demo-chart-top — barra horizontal: top produtos do período
        (em faturamento R$)
     ============================================================ */
  (function () {
    var el = document.getElementById('demo-chart-top');
    if (!el) return;
    var produtos = [
      'Massa Corrida Premium PVA 18L',
      'Tinta Acrílica Fosca 18L',
      'Esmalte Sintético Brilhante 3,6L',
      'Tinta Látex Branca 18L',
      'Selador Acrílico 18L'
    ];
    var valores = [28450, 24800, 19200, 16500, 12700];

    Plotly.newPlot(el, [{
      type: 'bar',
      orientation: 'h',
      x: valores,
      y: produtos,
      marker: {
        color: [NEXO.purple, NEXO.info, NEXO.amber, NEXO.green, NEXO.purpleD],
        line: { color: 'rgba(255,255,255,0.08)', width: 1 }
      },
      hovertemplate: '<b>%{y}</b><br>R$ %{x:,.0f}<extra></extra>'
    }], baseLayout({
      height: 220,
      margin: { l: 170, r: 24, t: 14, b: 34 },
      xaxis: { gridcolor: NEXO.grid, tickprefix: 'R$ ', tickformat: ',.0f', fixedrange: true, automargin: true },
      yaxis: { autorange: 'reversed', tickfont: { size: 10.5, color: NEXO.textMut }, fixedrange: true, automargin: true }
    }), Object.assign({}, baseConfig, { staticPlot: false }));
  })();
})();
