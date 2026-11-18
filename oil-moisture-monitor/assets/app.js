/* ============================================================
   油液微水在线监测系统 · 上位机逻辑
   运行形态：内嵌于 QtWebEngine(PyQt5) 桌面程序（免安装 .exe）
   数据源：宿主程序（Python/QtWebEngine）通过 window.bridge 推送
   离线兜底：无宿主且演示模式开启时，页面用内置模拟数据兜底
             （直接双击 html 也能预览界面）
   ============================================================ */
(function () {
  'use strict';

  /* ---------------- 常量 ---------------- */
  var CIRC = 2 * Math.PI * 52;              // 环形仪表周长
  var HIST_ROWS_MAX = 200;                  // 历史表最多渲染到 DOM 的行数（内存仍保存全量数据）
  var LOG_MAX = 300;                        // 通信日志最多保留条数
  var METRICS = {
    temp: { label: '温度',   min: -20,  max: 125,  dec: 1 },
    aw:   { label: '水活性', min: 0,    max: 1,    dec: 3 },
    ppm:  { label: '含水量', min: 0,    max: 1000, dec: 1 }
  };
  var LINK_LABEL = {
    WIFI5G: 'WiFi 5G', WIFI24G: 'WiFi 2.4G'
  };
  var TRANSPORT_LABEL = {
    wifi: 'WiFi 无线', demo: '演示数据', none: '未选择'
  };

  /* ---------------- 状态 ---------------- */
  var S = {
    mode: 'idle',            // idle | demo
    conn: { state: 'idle', name: '', ip: '', port: '', msg: '' },
    transport: 'none',       // wifi | ble | demo | none
    scanning: false,
    devices: [],
    connectedId: null,
    localDemo: false,        // 是否正在使用内置兜底模拟数据
    temp: NaN, aw: NaN, ppm: NaN,
    bat: NaN, lvl: NaN, rssi: NaN,
    cur: NaN, pwr: NaN,
    sOk: null,               // 传感器级状态：1=正常 0=失联（C5 判定，超时未收到 RS485 数据）
    fw: '',
    link: '', seq: null, addr: 1,
    packets: 0, lost: 0, lastSeq: null,
    rate: 0,
    paused: false,           // 暂停数据更新（冻结曲线/历史/卡片，后台仍收帧）
    stall: false,            // 数据停滞：已连接但超过 3s 无帧
    lastTime: null, prev: {},
    series: { temp: [], aw: [], ppm: [] },
    times: [],
    hist: [],
    rawHist: [],             // 原始 485 收发帧 {time,dir,hex,note}
    recId: 0,                // 本机记录号（1 起自增，永不复位；设备 seq 会 255→0 回绕，不能当编号）
    samples: 0,
    eis: { sets: [], view: 'bode', range: 'all', skipped: 0 },  // 阻抗谱：曲线集 + 视图 + 频率区间
    eisEval: { tand: NaN, acid: NaN, per: [] }    // 由阻抗谱反演的介损 / 酸值（per=逐组结果）
  };

  var SET = {
    refresh: 1000, window: 180,
    tempMax: 80, awMax: 0.80, ppmMax: 500,
    autoRec: true, sound: false, histMax: 20000,
    csvSplitMinutes: 0
  };
  var RAW_HIST_MAX = 20000;   // 原始帧历史上限（行数，TX/RX 各算一行）

  /* ---------------- 工具 ---------------- */
  function $(id) { return document.getElementById(id); }
  function fmt(v, dec) { return isFinite(v) ? Number(v).toFixed(dec) : '--'; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg, type) {
    var wrap = $('toastWrap');
    if (!wrap) return;
    var el = document.createElement('div');
    el.className = 'toast ' + (type || 'info');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () {
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 300);
    }, 2600);
  }

  /* 仅在文本真正变化时写入 DOM，减少重复写入开销 */
  function setText(el, v) {
    if (!el) return;
    var s = String(v);
    if (el.textContent !== s) el.textContent = s;
  }

  /* 数值滚动动画 */
  var animRAF = {};
  function animateValue(el, to, dec) {
    if (!el) return;
    if (!isFinite(to)) { el.textContent = '--'; return; }
    var from = parseFloat(el.textContent);
    if (!isFinite(from)) from = to;
    var key = el.id;
    if (animRAF[key]) cancelAnimationFrame(animRAF[key]);
    var t0 = performance.now(), dur = 420;
    function step(now) {
      var k = Math.max(0, Math.min(1, (now - t0) / dur));
      var e = 1 - Math.pow(1 - k, 3);
      el.textContent = (from + (to - from) * e).toFixed(dec);
      if (k < 1) animRAF[key] = requestAnimationFrame(step);
    }
    animRAF[key] = requestAnimationFrame(step);
  }

  /* 物理链路标识归一化 */
  function normLink(s) {
    if (!s) return '';
    var u = String(s).toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (u.indexOf('5G') >= 0 || u.indexOf('WIFI5') >= 0) return 'WIFI5G';
    if (u.indexOf('24') >= 0 || u.indexOf('2G') >= 0 || u.indexOf('WIFI2') >= 0) return 'WIFI24G';
    if (u.indexOf('ESPNOW') >= 0) return 'ESPNOW';
    if (u.indexOf('BLE') >= 0 || u.indexOf('BLUETOOTH') >= 0) return 'BLE';
    if (u.indexOf('WIFI') >= 0) return 'WIFI24G';
    return '';
  }

  /* ============================================================
     宿主桥 window.bridge
     ------------------------------------------------------------------
     宿主 → 页面（Python 通过 QWebEngineView.page().runJavaScript 调用）：
       window.bridge.setDevices(list)     // 刷新设备匹配页设备列表
       window.bridge.setScanning(bool)    // 扫描中/结束，控制雷达动画
       window.bridge.setConnState(obj)    // 连接状态与胶囊
       window.bridge.pushFrame(frame|str) // 一帧监测数据（唯一实时数据入口）
       window.bridge.pushLog(line)        // 追加一条通信日志
       window.bridge.setTransport(name)   // 'wifi'|'ble'|'demo'|'none'
       window.bridge.setConfig(obj)       // 宿主配置，如 {"auto_fullscreen":true}
     页面 → 宿主（宿主每 100ms 轮询）：
       window.bridge.post(cmdObj)         // 页面按钮把命令压入队列
       JSON.stringify(window.bridge.drainCommands())  // 宿主取走并清空
     命令格式：{cmd:'scan'} / {cmd:'connect',ip,port,id} /
               {cmd:'disconnect'} / {cmd:'demo',on} / {cmd:'save_csv',csv}
     ============================================================ */
  var _cmds = [];            // 待宿主取走的命令队列
  var _lastHostPoll = 0;     // 宿主最近一次轮询 drainCommands 的时间戳
  var _lastFrameAt = 0;      // 最近一次收到宿主数据帧的时间戳
  var _ready = false;        // DOM 是否已就绪
  var _autoFs = null;        // 宿主下发的"启动时自动全屏"状态（null=未知）
  var _pending = [];         // DOM 就绪前的宿主调用暂存

  function hasHost() {
    return _lastHostPoll > 0 && (Date.now() - _lastHostPoll) < 3000;
  }

  /* ---------- 数据帧解析（兼容 JSON 字符串与对象） ---------- */
  function toFrame(raw) {
    if (raw == null) return null;
    var o = raw;
    if (typeof raw === 'string') {
      var s = raw.trim();
      if (!s) return null;
      try { o = JSON.parse(s); }
      catch (e) { return parseTextFrame(s); }
    }
    if (!o || typeof o !== 'object') return null;
    return o;
  }

  /* 兼容固件调试文本格式：[recv] #123 T=27.60C AW=0.607 W=42.60ppm BAT=22.40V(85) link=WiFi5G */
  function parseTextFrame(line) {
    var m = line.match(/#\s*(\d+)\s+T=([-\d.]+)\s*C?\s+AW=([\d.]+)\s+W=([\d.]+)\s*ppm\s+BAT=([\d.]+)\s*V\s*\((\d+)\)\s*link=(\S+)/i);
    if (!m) return null;
    return {
      seq: parseInt(m[1], 10), t: parseFloat(m[2]), aw: parseFloat(m[3]),
      ppm: parseFloat(m[4]), bat: parseFloat(m[5]), lvl: parseFloat(m[6]),
      link: normLink(m[7])
    };
  }

  /* 调试/自检句柄：供宿主 /cli/js 命令行只读检查页面内部状态 */
  window.__app = {
    S: S, SET: SET,
    eis: null,             // 阻抗谱导入自检信息（applyEIS 后写入，/cli/js 断言用）
    buildCSV: null       // 页面加载末尾再挂上（buildCSV 定义在文件下方）
  };

  window.bridge = {    /* ------- 宿主 → 页面 ------- */
    setDevices: function (list) { guard(function () { applyDevices(list); }); },
    setScanning: function (on) { guard(function () { applyScanning(on); }); },
    setConnState: function (obj) { guard(function () { applyConnState(obj); }); },
    pushFrame: function (frame) { guard(function () { applyFrame(frame); }); },
    pushLog: function (line) { guard(function () { appendLog(line); }); },
    setTransport: function (name) { guard(function () { applyTransport(name); }); },
    setConfig: function (obj) { guard(function () { applyConfig(obj); }); },
    setPaused: function (on) { guard(function () { if (!!on !== S.paused) togglePause(); }); },
    setEIS: function (payload) { guard(function () { applyEIS(payload); }); },
    setProvState: function (d) { guard(function () { applyProvState(d); }); },

    /* ------- 页面 → 宿主（命令队列） ------- */
    post: function (cmdObj) {
      if (cmdObj && cmdObj.cmd) _cmds.push(cmdObj);
    },
    drainCommands: function () {
      _lastHostPoll = Date.now();          // 标记宿主存活
      var c = _cmds.slice(0);
      _cmds.length = 0;
      return c;
    }
  };

  /* DOM 未就绪前的宿主调用先暂存，就绪后统一执行 */
  function guard(fn) {
    if (_ready) {
      try { fn(); }
      catch (e) {
        // 不再静默：热路径异常必须留痕（console 对用户不可见，不影响体验），
        // 否则会出现 recordRow 文本节点 TypeError 那样"数据在收、界面假死"的隐形故障
        if (window.console && console.error) console.error('[bridge] handler error:', e);
      }
    }
    else _pending.push(fn);
  }

  /* ============================================================
     宿主 → 页面的内部处理
     ============================================================ */
  function applyDevices(list) {
    var arr = Array.isArray(list) ? list : [];
    S.devices = arr.map(function (d, i) {
      d = d || {};
      var id = (d.id != null && d.id !== '') ? String(d.id) : ('dev-' + i);
      return {
        id: id,
        name: d.name || ('OIL-SENSOR-' + (i + 1)),
        ip: d.ip || '',
        port: (d.port != null ? d.port : null),
        model: d.model || 'ESP32-C5',
        rssi: (d.rssi != null && isFinite(d.rssi)) ? +d.rssi : NaN,
        bat: (d.bat != null && isFinite(d.bat)) ? +d.bat : NaN,
        link: normLink(d.link)
      };
    });
    renderDevices();
  }

  function applyScanning(on) {
    S.scanning = !!on;
    updateScanUI();
    if (S.scanning) appendLog('开始扫描附近设备…');
    else appendLog('扫描结束，共发现 ' + S.devices.length + ' 个设备');
  }

  function applyConnState(obj) {
    obj = obj || {};
    S.conn = {
      state: obj.state || 'idle',
      name: obj.name || '',
      ip: obj.ip || '',
      port: (obj.port != null ? obj.port : ''),
      msg: obj.msg || ''
    };
    if (S.conn.state === 'connected') {
      var m = findDeviceByIp(S.conn.ip);
      if (m) S.connectedId = m.id;
      toast('已连接 ' + (S.conn.name || S.conn.ip), 'ok');
      appendLog('已连接：' + (S.conn.name || S.conn.ip) + (S.conn.port ? (':' + S.conn.port) : ''));
    } else if (S.conn.state === 'connecting') {
      appendLog('正在连接 ' + (S.conn.ip || '') + '…');
    } else if (S.conn.state === 'error') {
      S.connectedId = null;
      showError(S.conn.msg || '连接失败');
      appendLog('连接失败：' + (S.conn.msg || '未知错误'));
    } else if (S.conn.state === 'idle') {
      S.connectedId = null;
    }
    applyConnUI();
    applyDeviceStates();
    updateSilent();
  }

  function applyFrame(frame) {
    _lastFrameAt = Date.now();
    if (S.stall) { S.stall = false; }        // 帧恢复，清除停滞标记
    if (S.paused) return;                    // 暂停中：只标记存活，不更新显示
    // 宿主开始推流 → 关闭离线兜底
    if (S.localDemo) {
      stopLocalDemo();
      S.mode = 'idle';
      applyConnUI();
    }
    var d = toFrame(frame);
    if (d) ingest(d);
  }

  function applyTransport(name) {
    S.transport = name || 'none';
    updateTransportUI();
    if (S.transport === 'demo') {
      S.mode = 'demo';
    } else if (S.transport === 'wifi' || S.transport === 'ble') {
      // 宿主接管真实数据源
      S.mode = 'idle';
      stopLocalDemo();
    }
    applyConnUI();
    updateSilent();
  }

  /* 宿主配置：同步"启动时自动全屏"开关（容错：DOM 不存在时静默） */
  function applyConfig(obj) {
    if (typeof obj === 'string') {
      try { obj = JSON.parse(obj); } catch (e) { return; }
    }
    if (!obj || typeof obj !== 'object') return;
    if ('auto_fullscreen' in obj) {
      var v = !!obj.auto_fullscreen;
      if (v !== _autoFs) {                    // 值未变化则跳过，避免重复 set
        _autoFs = v;
        var el = $('setAutoFullscreen');
        if (el) el.checked = v;
      }
    }
  }

  function findDeviceByIp(ip) {
    if (!ip) return null;
    for (var i = 0; i < S.devices.length; i++) {
      if (S.devices[i].ip === ip) return S.devices[i];
    }
    return null;
  }

  /* ---------------- 图表 ---------------- */
  var chart = null;
  var chartOpt = null;        // 复用的图表 option，避免每帧重建
  function initChart() {
    if (typeof echarts === 'undefined') return;
    var el = $('chartMain');
    if (!el) return;
    chart = echarts.init(el, null, { renderer: 'canvas' });
    chartOpt = {
      animation: false,        // 关闭逐帧动画（实时刷新最大的 CPU 消耗来源）
      grid: { left: 58, right: 58, top: 34, bottom: 34 },
      tooltip: {
        trigger: 'axis', appendToBody: true,
        backgroundColor: 'rgba(255,255,255,.96)',
        borderColor: '#e2ebf6', borderWidth: 1,
        textStyle: { color: '#0a1628', fontSize: 12 },
        extraCssText: 'box-shadow:0 8px 30px rgba(10,22,40,.14);border-radius:10px;'
      },
      legend: { show: false },
      xAxis: {
        type: 'category', boundaryGap: false, data: [],
        axisLine: { lineStyle: { color: '#e2ebf6' } },
        axisTick: { show: false },
        axisLabel: { color: '#66799a', fontSize: 11, interval: 'auto' },
        splitLine: { show: false }
      },
      yAxis: [
        {
          type: 'value', scale: true, name: '℃ / ppm',
          nameTextStyle: { color: '#66799a', fontSize: 11, padding: [0, 0, 0, -24] },
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#66799a', fontSize: 11 },
          splitLine: { lineStyle: { color: '#f0f5fb' } }
        },
        {
          type: 'value', min: 0, max: 1, name: 'aw',
          nameTextStyle: { color: '#66799a', fontSize: 11, padding: [0, -18, 0, 0] },
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#66799a', fontSize: 11, formatter: function (v) { return v.toFixed(1); } },
          splitLine: { show: false }
        }
      ],
      series: [
        {
          name: '温度', type: 'line', yAxisIndex: 0, smooth: true,
          showSymbol: false, sampling: 'lttb', data: [],
          lineStyle: { width: 2, color: '#0a1628' },
          itemStyle: { color: '#0a1628' }
        },
        {
          name: '水活性', type: 'line', yAxisIndex: 1, smooth: true,
          showSymbol: false, sampling: 'lttb', data: [],
          lineStyle: { width: 2.4, color: '#1257d6' },
          itemStyle: { color: '#1257d6' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(18,87,214,.22)' },
              { offset: 1, color: 'rgba(18,87,214,0)' }
            ])
          }
        },
        {
          name: '含水量', type: 'line', yAxisIndex: 0, smooth: true,
          showSymbol: false, sampling: 'lttb', data: [],
          lineStyle: { width: 2.4, color: '#2f8bff' },
          itemStyle: { color: '#2f8bff' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(47,139,255,.20)' },
              { offset: 1, color: 'rgba(47,139,255,0)' }
            ])
          }
        }
      ]
    };
    chart.setOption(chartOpt, { notMerge: true });   // 初次全量建立
    window.addEventListener('resize', function () { chart.resize(); });
    syncSeries();
  }

  function has(m) {
    var b = document.querySelector('#segMetrics button[data-m="' + m + '"]');
    return b && b.classList.contains('on');
  }

  /* 按勾选状态切换曲线显示、Y 轴与轴向留白 */
  function refreshChartAxes() {
    if (!chartOpt) return;
    var t = has('temp'), a = has('aw'), p = has('ppm');
    chartOpt.yAxis[0].name = (t && p) ? '℃ / ppm' : (t ? '℃' : (p ? 'ppm' : ''));
    chartOpt.yAxis[1].show = a;
    chartOpt.grid.right = a ? 58 : 26;
    /* ECharts line series 无 show 属性（设置无效，曲线仍会渲染），
       必须用 legend.selected 控制系列显隐；legend.show=false 时该状态同样生效 */
    chartOpt.legend.selected = { '温度': t, '水活性': a, '含水量': p };
  }

  /* 根据 seg 选择显示哪些曲线（复用 option，增量更新） */
  function syncSeries() {
    if (!chart || !chartOpt) return;
    var win = SET.window;
    chartOpt.xAxis.data = S.times.slice(-win);
    chartOpt.series[0].data = S.series.temp.slice(-win);
    chartOpt.series[1].data = S.series.aw.slice(-win);
    chartOpt.series[2].data = S.series.ppm.slice(-win);
    refreshChartAxes();
    chart.setOption(chartOpt, { notMerge: false, lazyUpdate: true });
  }

  function pushChart() {
    if (!chart || !chartOpt) return;
    var win = SET.window;
    S.series.temp = S.series.temp.slice(-win);
    S.series.aw = S.series.aw.slice(-win);
    S.series.ppm = S.series.ppm.slice(-win);
    S.times = S.times.slice(-win);
    // 复用同一 option，仅替换数据后增量更新（不再重建 option 对象）
    chartOpt.xAxis.data = S.times;
    chartOpt.series[0].data = S.series.temp;
    chartOpt.series[1].data = S.series.aw;
    chartOpt.series[2].data = S.series.ppm;
    refreshChartAxes();
    chart.setOption(chartOpt, { notMerge: false, lazyUpdate: true });
  }

  /* ---------------- 阻抗谱（EIS）图表 ---------------- */
  var eisChart = null;
  // 9 组及以上必须给足不同色：原调色板只有 8 色，第 9 组回到第 1 组的蓝色，
  // 且后画的会整条盖住先画的，视觉上直接"少一条"
  var EIS_COLORS = ['#1257d6', '#e67e22', '#16a085', '#c0392b', '#8e44ad',
                    '#2c3e50', '#b89b1b', '#0e9594', '#d81b60', '#5d4037'];

  /* 仪器导出的文件名带 "(Freq,Zre,Zim,Z,phase)" 后缀，图例里 9 个长名字会
     挤成三行并压住坐标轴标题，显示时去掉括号部分，tooltip 也用它 */
  function eisName(s) {
    return String((s && s.name) || '').replace(/\s*\([^()]*\)\s*$/, '');
  }

  /* 坐标轴紧凑数字：|Z| 跨 10^5~10^11，完整千分位刻度最长 15 个字符，
     固定 grid.left 装不下会被画布左边缘裁掉（表现为刻度只剩后半截）；
     ≥1e5 或 <1e-3 用科学计数（1e11），短且不丢精度 */
  function axisNum(v) {
    var a = Math.abs(v);
    if (a >= 1e5 || (a > 0 && a < 1e-3)) {
      var e = Math.floor(Math.log10(a));
      var m = v / Math.pow(10, e);
      return (Math.abs(Math.abs(m) - 1) < 1e-6 ? '1' : m.toFixed(1)) + 'e' + e;
    }
    return String(Math.round(v * 1e6) / 1e6);
  }

  function initEisChart() {
    if (typeof echarts === 'undefined') return;
    var el = $('chartEis');
    if (!el) return;
    eisChart = echarts.init(el, null, { renderer: 'canvas' });
    window.addEventListener('resize', function () { if (eisChart) eisChart.resize(); });
    renderEis();
  }

  /* 宿主推送的导入结果：{sets:[{name,freq,zre,zim,z,phase}], skipped, dir} */
  function applyEIS(payload) {
    payload = payload || {};
    var sets = Array.isArray(payload.sets) ? payload.sets : [];
    S.eis.sets = sets.filter(function (s) {
      return s && Array.isArray(s.freq) && Array.isArray(s.zre) && Array.isArray(s.zim)
             && s.freq.length >= 2;
    });
    S.eis.skipped = payload.skipped || 0;
    renderEis();
    var st = $('eisStatus');
    if (S.eis.sets.length) {
      var pts = 0;
      S.eis.sets.forEach(function (s) { pts += s.freq.length; });
      setText(st, '已导入 ' + S.eis.sets.length + ' 个文件 · 共 ' + pts + ' 个频点'
              + (S.eis.skipped ? ' · 已跳过 ' + S.eis.skipped + ' 个非阻抗谱文件' : ''));
    } else {
      setText(st, '未找到有效阻抗谱数据'
              + (S.eis.skipped ? '（已跳过 ' + S.eis.skipped + ' 个非阻抗谱文件）' : ''));
    }
    // 介损 / 酸值：宿主对每一组分别反演，卡片显示各组均值，提示里给范围
    var ev = payload.eval || payload;
    S.eisEval.per = Array.isArray(ev.per_set) ? ev.per_set : [];
    S.eisEval.tand = isFinite(ev.tand) ? +ev.tand : NaN;
    S.eisEval.acid = isFinite(ev.acid) ? +ev.acid : NaN;
    updateEval();
    // 自检句柄：/cli/js 断言用
    window.__app.eis = {
      count: S.eis.sets.length, skipped: S.eis.skipped, view: S.eis.view,
      names: S.eis.sets.map(function (s) { return s.name; }),
      points: S.eis.sets.length ? S.eis.sets[0].freq.length : 0,
      tand: S.eisEval.tand, acid: S.eisEval.acid,
      per: S.eisEval.per
    };
  }

  /* 逐组结果的范围文案：min~max（同一量纲） */
  function perRange(key, dec) {
    var vals = (S.eisEval.per || []).map(function (r) { return +r[key]; })
      .filter(function (v) { return isFinite(v); });
    if (!vals.length) return '';
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    return lo.toFixed(dec) + '~' + hi.toFixed(dec);
  }

  /* 介损 / 酸值卡片：无模型结果时显示 --，有结果时按量程填充刻度条 */
  function updateEval() {
    var t = S.eisEval.tand, a = S.eisEval.acid;
    var n = (S.eisEval.per || []).length;
    setText($('vTand'), isFinite(t) ? t.toFixed(4) : '--');
    setText($('vAcid'), isFinite(a) ? a.toFixed(3) : '--');
    var g1 = $('gTand');
    if (g1) g1.style.width = isFinite(t) ? Math.min(100, Math.max(0, t / 0.30 * 100)).toFixed(1) + '%' : '0%';
    var g2 = $('gAcid');
    if (g2) g2.style.width = isFinite(a) ? Math.min(100, Math.max(0, a / 0.20 * 100)).toFixed(1) + '%' : '0%';
    setText($('hintTand'), isFinite(t)
      ? (n > 1 ? n + ' 组反演均值（' + perRange('tand', 4) + '）' : '由本次导入的阻抗谱反演得出')
      : '导入阻抗谱后自动测算');
    setText($('hintAcid'), isFinite(a)
      ? (n > 1 ? n + ' 组反演均值（' + perRange('acid', 3) + '）' : '由本次导入的阻抗谱反演得出')
      : '导入阻抗谱后自动测算');
    renderEvalLists();
  }

  /* 逐组结果小表：介损 / 酸值各一份，行顺序与导入的文件顺序一致 */
  function renderEvalLists() {
    var per = S.eisEval.per || [];
    [['listTand', 'tand', 4], ['listAcid', 'acid', 3]].forEach(function (it) {
      var box = $(it[0]);
      if (!box) return;
      if (!per.length) {
        box.innerHTML = '<div class="li"><span>暂无结果</span><b>--</b></div>';
        return;
      }
      box.innerHTML = per.map(function (r) {
        return '<div class="li"><span>' + esc(eisName(r)) + '</span><b>' +
               (+r[it[1]]).toFixed(it[2]) + '</b></div>';
      }).join('');
    });
  }

  /* 空态提示：未导入任何阻抗谱时显示「请点右上角导入数据」，导入后隐藏 */
  function updateEisEmpty() {
    var el = $('eisEmpty');
    if (el) el.classList.toggle('hide', S.eis.sets.length > 0);
  }

  /* 取某条谱在任意频率处的 |Z|（log10(f)-log10|Z| 线性插值，端点外取端点） */
  function eisZAt(pts, f) {
    if (f <= pts[0][0]) return pts[0][1];
    var last = pts.length - 1;
    if (f >= pts[last][0]) return pts[last][1];
    var lo = 0, hi = last;
    while (hi - lo > 1) {
      var mid = (lo + hi) >> 1;
      if (pts[mid][0] <= f) lo = mid; else hi = mid;
    }
    var t = (Math.log10(f) - Math.log10(pts[lo][0])) /
            (Math.log10(pts[hi][0]) - Math.log10(pts[lo][0]) || 1);
    return Math.pow(10, Math.log10(pts[lo][1]) +
                    t * (Math.log10(pts[hi][1]) - Math.log10(pts[lo][1])));
  }

  function renderEis() {
    updateEisEmpty();
    if (!eisChart) return;
    // 无数据时清空画布，只保留空态提示，避免空白坐标系干扰视觉
    if (!S.eis.sets.length) { eisChart.clear(); return; }
    var tipStyle = {
      backgroundColor: 'rgba(255,255,255,.96)', borderColor: '#e2ebf6', borderWidth: 1,
      textStyle: { color: '#0a1628', fontSize: 12 },
      extraCssText: 'box-shadow:0 8px 30px rgba(10,22,40,.14);border-radius:10px;'
    };
    var axLbl = { color: '#66799a', fontSize: 11 };
    var legend = { show: true, type: 'scroll', top: 6, itemGap: 10, itemWidth: 14,
                   textStyle: { color: '#33476b', fontSize: 11 } };
    /* 频率区间预设：|Z| 跨六个数量级，全频下 0.01Hz 只占几像素宽；
       ECharts 的 dataZoom 在 log 轴上按线性值算百分比，缩不到低频段，
       因此直接改 X 轴 min/max（确定可控） */
    var range = S.eis.range === 'low' ? [0.01, 1]
              : S.eis.range === 'vlow' ? [0.01, 0.1]
              : [0.01, 100000];
    var opt;
    if (S.eis.view === 'dev') {
      /* 偏差对比：9 组谱在全频段只差几个百分点，直接叠画必然重合。
         以各组在同一频点的中位数为参考，画 (|Z|i/|Z|ref-1)*100%，
         形状差异被放大到独立可辨的量级。 */
      var base = S.eis.sets.reduce(function (a, s) {
        return s.freq.length > a.freq.length ? s : a;
      }, S.eis.sets[0]);
      // 只取所有组共同覆盖的频段：更宽的组（如 100kHz 起的 sample01-6）若把
      // 频段撑到 100kHz，其余组只能端点外推，会画出一条假的 -100% 下坠尾巴
      var fLo = Math.max.apply(null, S.eis.sets.map(function (s) { return Math.min.apply(null, s.freq); }));
      var fHi = Math.min.apply(null, S.eis.sets.map(function (s) { return Math.max.apply(null, s.freq); }));
      var grid = base.freq.filter(function (f) { return f >= fLo && f <= fHi; })
        .sort(function (a, b) { return a - b; });
      var zpts = S.eis.sets.map(function (s) {
        var pts = [];
        for (var k = 0; k < s.freq.length; k++) {
          if (!(s.freq[k] > 0)) continue;
          var z = (s.z && s.z[k] > 0) ? s.z[k] : Math.sqrt(s.zre[k] * s.zre[k] + s.zim[k] * s.zim[k]);
          if (z > 0) pts.push([s.freq[k], z]);
        }
        pts.sort(function (a, b) { return a[0] - b[0]; });
        return pts;
      });
      var ref = grid.map(function (f) {
        var vals = zpts.map(function (p) { return eisZAt(p, f); }).sort(function (a, b) { return a - b; });
        var n = vals.length;
        return n % 2 ? vals[(n - 1) / 2] : Math.sqrt(vals[n / 2 - 1] * vals[n / 2]);
      });
      opt = {
        animation: false,
        grid: { left: 58, right: 24, top: 56, bottom: 46 },
        tooltip: Object.assign({ trigger: 'axis', appendToBody: true, valueFormatter: function (v) {
          return (v == null ? '--' : (+v).toFixed(1) + '%');
        } }, tipStyle),
        legend: legend,
        xAxis: { type: 'log', logBase: 10, min: range[0], max: range[1],
                 name: '频率 (Hz)', nameLocation: 'middle', nameGap: 28,
                 nameTextStyle: { color: '#66799a', fontSize: 11 },
                 axisLabel: axLbl, axisLine: { lineStyle: { color: '#e2ebf6' } },
                 splitLine: { lineStyle: { color: '#f0f5fb' } } },
        yAxis: { type: 'value', name: '偏差 (%)', nameGap: 20, scale: true,
                 nameTextStyle: { color: '#66799a', fontSize: 11 },
                 axisLabel: Object.assign({ formatter: function (v) { return (+v).toFixed(0) + '%'; } }, axLbl),
                 axisLine: { show: false }, axisTick: { show: false },
                 splitLine: { lineStyle: { color: '#f0f5fb' } } },
        series: S.eis.sets.map(function (s, i) {
          var color = EIS_COLORS[i % EIS_COLORS.length];
          // 只在组自身的测量频段内画：更宽的组（如 100kHz 起的 sample01-6）
          // 若对窄组做端点外推，会画出一条假的下坠尾巴
          var f0 = Math.min.apply(null, s.freq), f1 = Math.max.apply(null, s.freq);
          var data = [];
          grid.forEach(function (f, k) {
            if (f < f0 || f > f1) return;
            data.push([f, (eisZAt(zpts[i], f) / ref[k] - 1) * 100]);
          });
          return { name: eisName(s), type: 'line', data: data, showSymbol: false,
                   lineStyle: { width: 2, color: color }, itemStyle: { color: color } };
        })
      };
    } else if (S.eis.view === 'nyquist') {
      opt = {
        animation: false,
        grid: { left: 66, right: 30, top: 56, bottom: 46 },
        tooltip: Object.assign({ trigger: 'item', appendToBody: true }, tipStyle),
        legend: legend,
        // Zre / -Zim 物理上非负；原来 scale:true 会让 ECharts 把下界外扩到
        // -3e10 这种量级，左侧空掉一大块还把刻度挤出去
        xAxis: { type: 'value', min: 0, name: 'Zre (Ω)', nameLocation: 'middle', nameGap: 26,
                 nameTextStyle: { color: '#66799a', fontSize: 11 },
                 axisLabel: Object.assign({ formatter: axisNum }, axLbl),
                 axisLine: { lineStyle: { color: '#e2ebf6' } },
                 splitLine: { lineStyle: { color: '#f0f5fb' } } },
        yAxis: { type: 'value', min: 0, name: '-Zim (Ω)', nameGap: 20,
                 nameTextStyle: { color: '#66799a', fontSize: 11 },
                 axisLabel: Object.assign({ formatter: axisNum }, axLbl),
                 axisLine: { show: false }, axisTick: { show: false },
                 splitLine: { lineStyle: { color: '#f0f5fb' } } },
        series: S.eis.sets.map(function (s, i) {
          var color = EIS_COLORS[i % EIS_COLORS.length];
          var data = [];
          for (var k = 0; k < s.freq.length; k++) data.push([s.zre[k], -s.zim[k]]);
          return { name: eisName(s), type: 'line', data: data, symbol: 'circle', symbolSize: 5,
                   lineStyle: { width: 2, color: color }, itemStyle: { color: color } };
        })
      };
    } else {
      // 博德图：X=对数频率（升序），左 Y=|Z|（对数，实线），右 Y=phase（虚线同色）
      var series = [];
      S.eis.sets.forEach(function (s, i) {
        var color = EIS_COLORS[i % EIS_COLORS.length];
        var nm = eisName(s);
        var mag = [], ph = [];
        for (var k = 0; k < s.freq.length; k++) {
          if (!(s.freq[k] > 0)) continue;
          var z = (s.z && s.z[k] > 0) ? s.z[k] : Math.sqrt(s.zre[k] * s.zre[k] + s.zim[k] * s.zim[k]);
          var p = (s.phase && isFinite(s.phase[k])) ? s.phase[k]
                  : Math.atan2(s.zim[k], s.zre[k]) * 180 / Math.PI;
          if (z > 0) { mag.push([s.freq[k], z]); ph.push([s.freq[k], p]); }
        }
        mag.sort(function (a, b) { return a[0] - b[0]; });
        ph.sort(function (a, b) { return a[0] - b[0]; });
        // 幅值与相位共用图例名：点图例一次即可整组显隐（图例只占一行，不压图）
        series.push({ name: nm, type: 'line', yAxisIndex: 0, data: mag, showSymbol: false,
                      lineStyle: { width: 2, color: color }, itemStyle: { color: color } });
        series.push({ name: nm, type: 'line', yAxisIndex: 1, data: ph, showSymbol: false,
                      lineStyle: { width: 1.4, type: 'dashed', color: color, opacity: .55 },
                      itemStyle: { color: color } });
      });
      opt = {
        animation: false,
        grid: { left: 62, right: 58, top: 56, bottom: 46 },
        tooltip: Object.assign({ trigger: 'axis', appendToBody: true }, tipStyle),
        legend: legend,
        xAxis: { type: 'log', logBase: 10, min: range[0], max: range[1],
                 name: '频率 (Hz)', nameLocation: 'middle', nameGap: 28,
                 nameTextStyle: { color: '#66799a', fontSize: 11 },
                 axisLabel: axLbl, axisLine: { lineStyle: { color: '#e2ebf6' } },
                 splitLine: { lineStyle: { color: '#f0f5fb' } } },
        yAxis: [
          { type: 'log', logBase: 10, name: '|Z| (Ω)', nameGap: 20,
            nameTextStyle: { color: '#66799a', fontSize: 11 },
            axisLabel: Object.assign({ formatter: axisNum }, axLbl),
            axisLine: { show: false }, axisTick: { show: false },
            splitLine: { lineStyle: { color: '#f0f5fb' } } },
          { type: 'value', name: 'phase (°)',
            nameTextStyle: { color: '#66799a', fontSize: 11 },
            axisLabel: axLbl, axisLine: { show: false }, axisTick: { show: false },
            splitLine: { show: false } }
        ],
        series: series
      };
    }
    eisChart.setOption(opt, { notMerge: true });
  }

  /* ---------------- 仪表盘 ---------------- */
  function setRing(metric, value) {
    var c = document.querySelector('circle[data-ring="' + metric + '"]');
    if (!c) return;
    var m = METRICS[metric];
    var p = isFinite(value) ? (value - m.min) / (m.max - m.min) : 0;
    p = Math.max(0, Math.min(1, p));
    c.style.strokeDashoffset = (CIRC * (1 - p)).toFixed(1);
  }

  function setTrend(id, metric, value) {
    var el = $(id);
    if (!el) return;
    var prev = S.prev[metric];
    if (!isFinite(prev) || !isFinite(value)) { el.textContent = '—'; el.className = 'trend flat'; return; }
    var d = value - prev;
    var eps = metric === 'aw' ? 0.001 : 0.05;
    if (d > eps)      { el.textContent = '▲ +' + Math.abs(d).toFixed(METRICS[metric].dec); el.className = 'trend up'; }
    else if (d < -eps) { el.textContent = '▼ -' + Math.abs(d).toFixed(METRICS[metric].dec); el.className = 'trend down'; }
    else              { el.textContent = '— 稳定'; el.className = 'trend flat'; }
  }

  function setCardAlarm(metric, on) {
    var nodes = document.querySelectorAll('[data-metric="' + metric + '"]');
    Array.prototype.forEach.call(nodes, function (n) { n.classList.toggle('alarm', !!on); });
  }

  /* 是否处于“有数据源”状态：演示模式 或 已连接 */
  function isActive() {
    return S.mode === 'demo' || S.conn.state === 'connected';
  }

  function updateAlarm() {
    var bar = $('alarmBar'), txt = $('alarmText');
    if (!isActive()) {
      setCardAlarm('temp', false); setCardAlarm('aw', false); setCardAlarm('ppm', false);
      if (bar) { bar.classList.remove('alarm'); bar.classList.add('idle'); }
      if (txt) txt.textContent = '尚未接入数据源，请到「设备匹配」扫描连接，或开启演示模式';
      return false;
    }
    if (bar) bar.classList.remove('idle');

    var msgs = [];
    if (S.sOk === 0) msgs.push('传感器失联（RS485 超时无响应，显示为最后读数）');
    if (isFinite(S.temp) && S.temp > SET.tempMax)  msgs.push('温度 ' + fmt(S.temp, 1) + '℃ 超过上限 ' + SET.tempMax + '℃');
    if (isFinite(S.aw)   && S.aw   > SET.awMax)    msgs.push('水活性 ' + fmt(S.aw, 3) + 'aw 超过上限 ' + SET.awMax + 'aw');
    if (isFinite(S.ppm)  && S.ppm  > SET.ppmMax)   msgs.push('含水量 ' + fmt(S.ppm, 1) + 'ppm 超过上限 ' + SET.ppmMax + 'ppm');

    setCardAlarm('temp', isFinite(S.temp) && S.temp > SET.tempMax);
    setCardAlarm('aw',   isFinite(S.aw)   && S.aw   > SET.awMax);
    setCardAlarm('ppm',  isFinite(S.ppm)  && S.ppm  > SET.ppmMax);

    if (msgs.length) {
      if (bar) bar.classList.add('alarm');
      if (txt) txt.textContent = '报警：' + msgs.join('；');
    } else {
      if (bar) bar.classList.remove('alarm');
      if (txt) txt.textContent = '系统运行正常，各项指标均在安全范围内';
    }
    return msgs.length > 0;
  }

  /* 静默态切换：断线时数据卡片转灰、不报警 */
  function updateSilent() {
    var active = isActive();
    var page = document.querySelector('.page');
    if (page) page.classList.toggle('data-idle', !active);
    updateAlarm();
  }

  /* ---------------- 状态面板 ---------------- */
  function updateStatus() {
    var r = $('sRssi'); setText(r, isFinite(S.rssi) ? S.rssi : '--');
    var bars = document.querySelectorAll('#rssiBars i');
    var n = 0;
    if (isFinite(S.rssi)) n = S.rssi >= -50 ? 5 : S.rssi >= -60 ? 4 : S.rssi >= -70 ? 3 : S.rssi >= -80 ? 2 : 1;
    Array.prototype.forEach.call(bars, function (b, i) { b.classList.toggle('on', i < n); });

    var bv = $('sBatV'); setText(bv, isFinite(S.bat) ? fmt(S.bat, 2) : '--');
    var bp = $('sBatPct'); setText(bp, isFinite(S.lvl) ? S.lvl : '--');
    var ca = $('sCurA'); setText(ca, isFinite(S.cur) ? fmt(S.cur, 3) : '--');
    var bw = $('sBatPwr'); setText(bw, isFinite(S.pwr) ? fmt(S.pwr, 2) : '--');
    // 传感器级状态（C5 判定：超时未收到 RS485 数据 → 失联）
    var so = $('sSensorOk');
    if (so) {
      so.textContent = S.sOk === null ? '--' : (S.sOk ? '正常' : '失联');
      so.style.color = S.sOk === null ? '' : (S.sOk ? '#16a34a' : '#dc2626');
      so.style.fontWeight = S.sOk ? '' : '700';
    }
    // 电流/功率条
    var cb = $('sCurBar');
    if (cb) { cb.style.width = isFinite(S.cur) ? Math.min(100, (Math.abs(S.cur) / 2.0) * 100).toFixed(0) + '%' : '0%'; }
    var pb2 = $('sPwrBar');
    if (pb2) { pb2.style.width = isFinite(S.pwr) ? Math.min(100, (Math.abs(S.pwr) / 10.0) * 100).toFixed(0) + '%' : '0%'; }
    var pv = isFinite(S.bat) ? Math.max(0, Math.min(1, (S.bat - 18) / (25.2 - 18))) : 0;
    var bb = $('sBatBar');
    if (bb) {
      bb.style.width = (pv * 100).toFixed(0) + '%';
      bb.classList.toggle('low', pv < 0.25);
    }
    var pb = $('sBatPctBar');
    if (pb) {
      pb.style.width = (isFinite(S.lvl) ? S.lvl : 0) + '%';
      pb.classList.toggle('low', isFinite(S.lvl) && S.lvl < 25);
    }

    var pk = $('sPackets'); setText(pk, S.packets);
    var sq = $('sSeq'); setText(sq, S.seq === null ? '--' : S.seq);
    var loss = S.packets > 0 ? (S.lost / (S.packets + S.lost)) * 100 : 0;
    var ls = $('sLoss'); setText(ls, loss.toFixed(1));
    var tm = $('sTime'); setText(tm, S.lastTime || '--:--:--');
    var ad = $('sAddr'); setText(ad, S.addr);
    var rt = $('sRate'); setText(rt, S.rate.toFixed(1));
    // 用户要求：加一个直观的累计接收帧计数（复用 S.packets，永不回绕）
    var fr = $('sFrames'); if (fr) setText(fr, S.packets);
    // 单屏卡片：更新时刻 + 含水量刻度条
    var lv = $('liveStamp'); if (lv) setText(lv, S.lastTime || '--:--:--');
    var pm = $('ppmBar');
    if (pm) pm.style.width = isFinite(S.ppm) ? Math.min(100, Math.max(0, S.ppm / 1000 * 100)).toFixed(1) + '%' : '0%';

    var modes = document.querySelectorAll('#linkModes span');
    Array.prototype.forEach.call(modes, function (m) {
      m.classList.toggle('on', m.dataset.mode === S.link);
    });
  }

  /* 顶栏连接状态胶囊 */
  function applyConnUI() {
    var pill = $('connPill'), main = $('connPillMain'), sub = $('connPillSub');
    if (pill) pill.classList.remove('on', 'demo', 'wait', 'err');
    var st = S.conn.state, name = '未连接', extra = '';
    if (S.paused) {
      if (pill) pill.classList.add('wait');
      name = '已暂停'; extra = '数据更新已冻结，点击「启动」恢复';
    } else if (S.mode === 'demo') {
      if (pill) pill.classList.add('demo');
      name = '演示模式'; extra = '模拟数据';
    } else if (st === 'connected') {
      if (pill) pill.classList.add('on');
      name = S.conn.name || '已连接';
      extra = LINK_LABEL[S.link] || S.conn.ip || '';
    } else if (st === 'scanning') {
      if (pill) pill.classList.add('wait');
      name = '扫描中'; extra = '正在搜索设备';
    } else if (st === 'connecting') {
      if (pill) pill.classList.add('wait');
      name = '连接中'; extra = S.conn.msg || S.conn.ip || '';
    } else if (st === 'error') {
      if (pill) pill.classList.add('err');
      name = '连接失败'; extra = S.conn.msg || '';
    }
    if (main) main.textContent = name;
    if (sub) sub.textContent = extra;
    if (pill) pill.title = name + (extra ? (' ' + extra) : '');

    // 演示按钮状态同步：演示中小圆点亮起、文字变「演示中」
    var demoOn = (S.mode === 'demo');
    var dd = $('demoDot'), dt = $('btnDemoText'), db = $('btnDemo');
    if (dd) dd.classList.toggle('on', demoOn);
    if (dt) dt.textContent = demoOn ? '演示中' : '演示';
    if (db) db.classList.toggle('demo-on', demoOn);

    var fs = $('footSource');
    if (fs) {
      fs.textContent = '数据源：' + (S.mode === 'demo'
        ? '演示模式（模拟数据）'
        : (st === 'connected' ? ('设备 ' + (S.conn.name || S.conn.ip)) : '未连接'));
    }
  }

  /* ---------------- 设备匹配页 ---------------- */
  var WIFI_ICON = '<svg viewBox="0 0 24 24" width="17" height="17">' +
    '<path d="M12 18.5a1.7 1.7 0 1 0 0 3.4 1.7 1.7 0 0 0 0-3.4z" fill="currentColor"/>' +
    '<path d="M4.4 11.9a11 11 0 0 1 15.2 0" stroke="currentColor" stroke-width="1.9" fill="none" stroke-linecap="round"/>' +
    '<path d="M7.5 15a6.8 6.8 0 0 1 9 0" stroke="currentColor" stroke-width="1.9" fill="none" stroke-linecap="round"/>' +
    '</svg>';

  function rssiLevel(r) {
    if (!isFinite(r)) return 0;
    return r >= -55 ? 4 : r >= -65 ? 3 : r >= -75 ? 2 : r >= -88 ? 1 : 0;
  }

  function emptyStateHTML() {
    return '<div class="dev-empty">' +
      '<svg viewBox="0 0 150 108" width="150" height="108" fill="none">' +
        '<circle cx="66" cy="50" r="30" stroke="#dbe6f4" stroke-width="1.6" stroke-dasharray="4 6"/>' +
        '<circle cx="66" cy="50" r="45" stroke="#e8f0fa" stroke-width="1.4" stroke-dasharray="3 7"/>' +
        '<rect x="51" y="38" width="30" height="24" rx="5" stroke="#c9d6e8" stroke-width="1.8"/>' +
        '<path d="M59 46h14M59 52h9" stroke="#c9d6e8" stroke-width="1.8" stroke-linecap="round"/>' +
        '<path d="M104 34a30 30 0 0 1 0 32" stroke="#9dc2ff" stroke-width="1.9" stroke-linecap="round"/>' +
        '<path d="M114 25a44 44 0 0 1 0 50" stroke="#cfe0ff" stroke-width="1.9" stroke-linecap="round"/>' +
      '</svg>' +
      '<p class="de-title">暂未发现设备</p>' +
      '<p>点击左侧「扫描设备」，或手动输入设备 IP 直接连接</p>' +
    '</div>';
  }

  function devCardHTML(d) {
    var lvl = rssiLevel(d.rssi);
    var bars = '<span class="di-bl">' +
      [1, 2, 3, 4].map(function (i) { return '<i class="' + (i <= lvl ? 'on' : '') + '"></i>'; }).join('') +
      '</span>';
    var linkTxt = LINK_LABEL[d.link] || d.link || '—';
    var bat = isFinite(d.bat) ? (d.bat + '%') : '--';
    return '<div class="dev-item" data-id="' + esc(d.id) + '">' +
      '<div class="di-head">' +
        '<span class="di-icon">' + WIFI_ICON + '</span>' +
        '<span class="di-name" title="' + esc(d.name) + '">' + esc(d.name) + '</span>' +
        '<span class="di-link">' + esc(linkTxt) + '</span>' +
      '</div>' +
      '<div class="di-meta">' +
        '<span>IP ' + esc(d.ip || '--') + '</span>' +
        '<span>信号 ' + bars + '</span>' +
        '<span>电量 ' + bat + '</span>' +
      '</div>' +
      '<div class="di-foot">' +
        '<span class="di-model" title="' + esc(d.model) + '">' + esc(d.model) + '</span>' +
        '<button class="btn btn-primary btn-sm di-connect" data-id="' + esc(d.id) + '">连接</button>' +
      '</div>' +
      '<span class="di-badge">已连接</span>' +
    '</div>';
  }

  var _devFp = null;         // 设备列表指纹，未变化时不重建 DOM
  function renderDevices() {
    var wrap = $('devList');
    if (!wrap) return;
    var cnt = $('devCount');
    if (cnt) cnt.textContent = S.devices.length + ' 个';

    // 廉价指纹：仅拼接影响展示的关键字段
    var fp = JSON.stringify(S.devices.map(function (d) {
      return d.id + '|' + d.ip + '|' + d.rssi + '|' + d.link;
    }));
    if (fp === _devFp) { applyDeviceStates(); return; }   // 列表无变化，跳过重建
    _devFp = fp;

    if (!S.devices.length) {
      wrap.innerHTML = emptyStateHTML();
      return;
    }
    wrap.innerHTML = S.devices.map(devCardHTML).join('');
    applyDeviceStates();
  }

  function applyDeviceStates() {
    var items = document.querySelectorAll('#devList .dev-item');
    Array.prototype.forEach.call(items, function (it) {
      var id = it.getAttribute('data-id');
      it.classList.remove('connected', 'connecting');
      var btn = it.querySelector('.di-connect');
      if (btn) { btn.classList.remove('loading'); btn.textContent = '连接'; btn.disabled = false; }
      if (S.conn.state === 'connected' && id === S.connectedId) {
        it.classList.add('connected');
        if (btn) { btn.textContent = '已连接'; btn.disabled = true; }
      } else if (S.conn.state === 'connecting' && id === S.connectedId) {
        it.classList.add('connecting');
        if (btn) { btn.classList.add('loading'); btn.textContent = '连接中…'; }
      }
    });
  }

  function updateScanUI() {
    var radar = $('radar'), st = $('radarStatus'), btn = $('btnScan'), bt = $('btnScanText');
    if (radar) radar.classList.toggle('on', S.scanning);
    if (btn) { btn.classList.toggle('loading', S.scanning); btn.disabled = S.scanning; }
    if (bt) bt.textContent = S.scanning ? '扫描中…' : '扫描设备';
    if (st) {
      if (S.scanning) st.textContent = S.devices.length
        ? ('正在搜索… 已发现 ' + S.devices.length + ' 个设备')
        : '正在搜索附近设备，请稍候…';
      else st.textContent = S.devices.length
        ? ('扫描完成，共发现 ' + S.devices.length + ' 个设备')
        : '点击「扫描设备」开始搜索';
    }
  }

  function updateTransportUI() {
    var el = $('matchTransport');
    if (el) el.textContent = '链路：' + (TRANSPORT_LABEL[S.transport] || '未选择');
  }

  var errTimer = null;
  function showError(msg) {
    var el = $('matchError');
    if (!el) return;
    if (!msg) { el.classList.remove('show'); el.textContent = ''; return; }
    el.textContent = msg;
    el.classList.add('show');
    if (errTimer) clearTimeout(errTimer);
    errTimer = setTimeout(function () { el.classList.remove('show'); }, 6000);
  }

  /* ---------------- 通信日志 ---------------- */
  var logs = [];
  function appendLog(line) {
    if (line == null) return;
    var ts = new Date().toTimeString().slice(0, 8);
    logs.push({ t: ts, s: String(line) });
    if (logs.length > LOG_MAX) logs = logs.slice(-LOG_MAX);
    renderLogs();
  }
  function renderLogs() {
    var el = $('logList');
    if (!el) return;
    if (!logs.length) { el.innerHTML = '<p class="log-empty">暂无日志</p>'; return; }
    el.innerHTML = logs.slice(-80).map(function (l) {
      return '<p><span class="lg-time">' + l.t + '</span>' + esc(l.s) + '</p>';
    }).join('');
    el.scrollTop = el.scrollHeight;
  }

  /* ---------------- 数据入口 ---------------- */
  var rateWin = { t0: 0, n: 0 };
  function ingest(d) {
    var now = new Date();
    var ts = now.toTimeString().slice(0, 8);

    S.prev.temp = S.temp; S.prev.aw = S.aw; S.prev.ppm = S.ppm;

    if (d.t   !== undefined && isFinite(d.t))   S.temp = +d.t;
    if (d.aw  !== undefined && isFinite(d.aw))  S.aw   = +d.aw;
    if (d.ppm !== undefined && isFinite(d.ppm)) S.ppm  = +d.ppm;
    if (d.bat !== undefined && isFinite(d.bat)) S.bat  = +d.bat;
    if (d.lvl !== undefined && isFinite(d.lvl)) S.lvl  = +d.lvl;
    if (d.rssi !== undefined && isFinite(d.rssi)) S.rssi = +d.rssi;
    if (d.cur !== undefined && isFinite(d.cur)) S.cur = +d.cur;
    if (d.pwr !== undefined && isFinite(d.pwr)) S.pwr = +d.pwr;
    if (d.s_ok !== undefined && d.s_ok !== null) S.sOk = (+d.s_ok) ? 1 : 0;
    if (d.fw !== undefined) S.fw = String(d.fw || '');
    if (d.link) S.link = normLink(d.link) || d.link;
    if (d.addr !== undefined) S.addr = d.addr;
    if (d.seq !== undefined && d.seq !== null) {
      S.seq = d.seq;
      if (S.lastSeq !== null && d.seq > S.lastSeq && d.seq - S.lastSeq > 1) {
        S.lost += (d.seq - S.lastSeq - 1);
      }
      S.lastSeq = d.seq;
    }

    S.packets++;
    S.samples++;
    S.lastTime = ts;

    // 数据速率（pkt/s）
    var wn = now.getTime();
    if (rateWin.t0 === 0) rateWin.t0 = wn;
    rateWin.n++;
    if (wn - rateWin.t0 >= 1000) {
      S.rate = rateWin.n * 1000 / (wn - rateWin.t0);
      rateWin.t0 = wn; rateWin.n = 0;
    }

    // 仪表盘
    animateValue($('vTemp'), S.temp, 1);
    animateValue($('vAw'), S.aw, 3);
    animateValue($('vPpm'), S.ppm, 1);
    setRing('temp', S.temp); setRing('aw', S.aw); setRing('ppm', S.ppm);
    setTrend('tTemp', 'temp', S.temp);
    setTrend('tAw', 'aw', S.aw);
    setTrend('tPpm', 'ppm', S.ppm);

    // 曲线
    S.series.temp.push(isFinite(S.temp) ? +S.temp.toFixed(2) : null);
    S.series.aw.push(isFinite(S.aw) ? +S.aw.toFixed(3) : null);
    S.series.ppm.push(isFinite(S.ppm) ? +S.ppm.toFixed(1) : null);
    S.times.push(ts);
    pushChart();

    // 历史
    var bad = updateAlarm();
    if (SET.autoRec) recordRow(ts);
    recordRaw(ts, d);
    updateStatus();

    if (bad && SET.sound) beep();
  }

  var lastBeep = 0;
  function beep() {
    var now = Date.now();
    if (now - lastBeep < 3000) return;
    lastBeep = now;
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.type = 'sine';
      g.gain.setValueAtTime(0.12, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.35);
    } catch (e) { /* 忽略音频异常 */ }
  }

  /* ---------------- 历史记录 ---------------- */
  function statusTag() {
    if (isFinite(S.temp) && S.temp > SET.tempMax) return '<span class="tag bad">温度超限</span>';
    if (isFinite(S.aw)   && S.aw   > SET.awMax)   return '<span class="tag bad">水活性超限</span>';
    if (isFinite(S.ppm)  && S.ppm  > SET.ppmMax)  return '<span class="tag bad">含水量超限</span>';
    return '<span class="tag ok">正常</span>';
  }

  /* 完整时间戳：yyyy-MM-dd HH:mm:ss.SSS（2Hz 采样同秒多条也能排序） */
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function pad3(n) { return (n < 10 ? '00' : (n < 100 ? '0' : '')) + n; }
  function fullStamp(dt) {
    return dt.getFullYear() + '-' + pad2(dt.getMonth() + 1) + '-' + pad2(dt.getDate()) + ' ' +
      pad2(dt.getHours()) + ':' + pad2(dt.getMinutes()) + ':' + pad2(dt.getSeconds()) + '.' +
      pad3(dt.getMilliseconds());
  }
  /* 数据分类（与状态标签同阈值）：正常 / 温度超限 / 水活性超限 / 含水量超限 / 失联 */
  function classOf(t, aw, ppm, sOk) {
    if (sOk === 0) return '失联';
    if (isFinite(t) && t > SET.tempMax) return '温度超限';
    if (isFinite(aw) && aw > SET.awMax) return '水活性超限';
    if (isFinite(ppm) && ppm > SET.ppmMax) return '含水量超限';
    return '正常';
  }
  function sOkText(v) {
    if (v === 1) return '正常';
    if (v === 0) return '失联';
    return '';
  }

  function recordRow(ts) {
    S.recId += 1;
    var cls = classOf(S.temp, S.aw, S.ppm, S.sOk);
    S.hist.push({
      id: S.recId, stamp: fullStamp(new Date()), time: ts,
      seq: S.seq, temp: S.temp, aw: S.aw, ppm: S.ppm,
      bat: S.bat, cur: S.cur, pwr: S.pwr, fw: S.fw, link: S.link,
      sOk: S.sOk, cls: cls
    });
    if (S.hist.length > SET.histMax) S.hist = S.hist.slice(-SET.histMax);

    var body = $('histBody');
    if (!body) return;
    var empty = body.querySelector('.empty-row');
    if (empty) empty.remove();

    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + S.recId + '</td>' +
      '<td>' + ts + '</td>' +
      '<td>' + fmt(S.temp, 1) + '</td>' +
      '<td>' + fmt(S.aw, 3) + '</td>' +
      '<td>' + fmt(S.ppm, 1) + '</td>' +
      '<td>' + fmt(S.bat, 2) + '</td>' +
      '<td>' + fmt(S.cur, 3) + '</td>' +
      '<td>' + fmt(S.pwr, 2) + '</td>' +
      '<td>' + esc(S.fw || '-') + '</td>' +
      '<td>' + (LINK_LABEL[S.link] || S.link || '-') + '</td>' +
      '<td>' + statusTag() + '</td>';
    body.insertBefore(tr, body.firstChild);
    // DOM 最多保留最近 HIST_ROWS_MAX 行（数据仍全量保存在 S.hist，导出不受影响）
    // 注意必须用 lastElementChild：tbody 内 HTML 换行会产生文本节点，
    // 用 lastChild 取到文本节点时 .classList 为 undefined 会抛 TypeError
    while (body.children.length > HIST_ROWS_MAX && body.lastElementChild && !body.lastElementChild.classList.contains('empty-row')) {
      body.removeChild(body.lastElementChild);
    }
    updateHistCount();
  }

  /* ---------------- 原始 485 收发帧 ---------------- */
  function recordRaw(ts, d) {
    if (d.rtx === undefined && d.rrx === undefined) return;   // 无原始帧字段（演示/旧固件）
    var body = $('rawBody');
    if (body) { var e = body.querySelector('.empty-row'); if (e) e.remove(); }
    pushRawRow(ts, 'TX', String(d.rtx || ''), 'Modbus 请求（读 0x0000 起 6 寄存器）');
    if (d.rrx) pushRawRow(ts, 'RX', String(d.rrx), '传感器响应，CRC 校验通过');
    else if (d.rtx) pushRawRow(ts, 'RX', '', '超时无响应（传感器失联或未接）');
    while (S.rawHist.length > RAW_HIST_MAX) S.rawHist.splice(0, S.rawHist.length - RAW_HIST_MAX);
    if (body) {
      // 同 recordRow：必须用 lastElementChild 避开 tbody 内文本节点
      while (body.children.length > HIST_ROWS_MAX && body.lastElementChild && !body.lastElementChild.classList.contains('empty-row')) {
        body.removeChild(body.lastElementChild);
      }
    }
    updateHistCount();
  }

  function pushRawRow(ts, dir, hex, note) {
    S.rawHist.push({ time: ts, dir: dir, hex: hex, note: note });
    var body = $('rawBody');
    if (!body) return;
    var tr = document.createElement('tr');
    var dirTag = dir === 'TX' ? '<span class="tag ok">TX 发</span>' : (hex ? '<span class="tag ok">RX 收</span>' : '<span class="tag bad">RX 缺</span>');
    tr.innerHTML = '<td>' + ts + '</td><td>' + dirTag + '</td>' +
      '<td style="font-family:Consolas,monospace;letter-spacing:.5px">' +
      (hex ? esc(spacedHex(hex)) : '<span style="color:#dc2626">（无数据）</span>') + '</td>' +
      '<td>' + (hex ? (hex.length / 2) + ' B' : '0 B') + '</td>' +
      '<td>' + esc(note) + '</td>';
    body.insertBefore(tr, body.firstChild);
  }

  /* HEX 串按字节加空格，便于阅读 */
  function spacedHex(h) {
    var out = [];
    for (var i = 0; i < h.length; i += 2) out.push(h.substr(i, 2));
    return out.join(' ');
  }

  function updateHistCount() {
    var raw = $('histWrapRaw') && $('histWrapRaw').style.display !== 'none';
    var hc = $('histCount');
    setText(hc, raw ? ('共 ' + S.rawHist.length + ' 帧') : ('共 ' + S.hist.length + ' 条'));
  }

  function switchHistMode(mode) {
    var raw = mode === 'raw';
    var wp = $('histWrapParsed'), wr = $('histWrapRaw');
    if (wp) wp.style.display = raw ? 'none' : '';
    if (wr) wr.style.display = raw ? '' : 'none';
    var btns = document.querySelectorAll('#histModeSeg button');
    Array.prototype.forEach.call(btns, function (b) {
      b.classList.toggle('on', (b.dataset.hmode === 'raw') === raw);
    });
    updateHistCount();
  }

  function buildCSV() {
    var head = '记录号,时间,温度(℃),水活性(aw),含水量(ppm),电池(V),电流(A),功率(W),' +
               '链路,固件,传感器状态,数据分类,设备序号\n';
    var rows = S.hist.map(function (r) {
      return [
        r.id,
        r.stamp || r.time,
        fmt(r.temp, 1), fmt(r.aw, 3), fmt(r.ppm, 1),
        fmt(r.bat, 2), fmt(r.cur, 3), fmt(r.pwr, 2),
        r.link || '', r.fw || '',
        sOkText(r.sOk),
        r.cls || classOf(r.temp, r.aw, r.ppm, r.sOk),
        r.seq === null || r.seq === undefined ? '' : r.seq
      ].join(',');
    }).join('\n');
    return '\uFEFF' + head + rows;
  }

  /* 导出：优先交给宿主弹保存对话框；无宿主时浏览器直接下载兜底 */
  function exportCSV() {
    if (!S.hist.length) { toast('暂无可导出的数据', 'err'); return; }
    var csv = buildCSV();
    window.bridge.post({ cmd: 'save_csv', csv: csv });
    appendLog('请求保存 CSV（' + S.hist.length + ' 条）');
    if (!hasHost()) browserDownload(csv);      // 宿主不存在时的兜底
    toast('已导出 ' + S.hist.length + ' 条记录', 'ok');
  }

  function browserDownload(csv) {
    var blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'oil_moisture_' + new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-') + '.csv';
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  /* ---------------- 暂停 / 启动 ---------------- */
  function togglePause() {
    S.paused = !S.paused;
    var txt = $('btnPauseText');
    if (txt) txt.textContent = S.paused ? '启动' : '暂停';
    var btn = $('btnPause');
    if (btn) btn.classList.toggle('btn-primary', S.paused);
    if (S.paused) {
      appendLog('数据更新已暂停（后台仍保持连接与收帧）');
      toast('已暂停，曲线/历史/卡片冻结', 'info');
    } else {
      _lastFrameAt = Date.now();   // 避免恢复瞬间触发停滞告警
      appendLog('数据更新已恢复');
      toast('已恢复实时更新', 'ok');
    }
    applyConnUI();
  }

  /* 数据停滞看门狗：已连接但超过 3s 无帧 → 速率归零 + 日志提示（重连由宿主负责） */
  var _stallLogged = false;
  function watchdogTick() {
    if (S.paused || S.localDemo) return;   // 暂停或内置兜底数据不参与停滞判定
    var active = (S.conn.state === 'connected');
    if (!active) { _stallLogged = false; return; }
    var silentFor = Date.now() - _lastFrameAt;
    if (_lastFrameAt > 0 && silentFor > 3000) {
      if (!S.stall) {
        S.stall = true;
        S.rate = 0;
        updateStatus();
      }
      if (!_stallLogged) {
        _stallLogged = true;
        appendLog('超过 3 秒未收到数据帧，链路可能停滞（宿主机将自动重连）');
      }
    } else {
      _stallLogged = false;
    }
  }

  /* ---------------- 演示模式与离线兜底 ---------------- */
  var demoTimer = null;       // 内置模拟数据定时器（兜底）
  var demoWatchTimer = null;  // 3 秒数据看门狗
  var demoBase = { t: 27.6, aw: 0.607, ppm: 42.6, bat: 22.4 };
  var demoDrift = { t: 0, aw: 0, ppm: 0 };
  var demoSeq = 0;

  function startLocalDemo() {
    if (demoTimer) return;
    S.localDemo = true;
    demoSeq = 0;
    S.packets = 0; S.lost = 0; S.lastSeq = null; S.seq = null;
    appendLog('未收到宿主数据，启用内置模拟数据兜底');
    demoStep();
    demoTimer = setInterval(demoStep, SET.refresh);
  }
  function stopLocalDemo() {
    if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
    S.localDemo = false;
  }
  /* 演示模式开启后，3 秒内没有收到任何数据帧 → 内置模拟数据兜底 */
  function startDemoWatch() {
    if (demoWatchTimer) clearTimeout(demoWatchTimer);
    var t0 = Date.now();
    demoWatchTimer = setTimeout(function () {
      if (_lastFrameAt < t0) startLocalDemo();
      else stopLocalDemo();
    }, 3000);
  }
  function stopDemoWatch() {
    if (demoWatchTimer) { clearTimeout(demoWatchTimer); demoWatchTimer = null; }
  }

  function demoStep() {
    demoDrift.t += (Math.random() - 0.5) * 0.35;
    demoDrift.aw += (Math.random() - 0.5) * 0.012;
    demoDrift.ppm += (Math.random() - 0.5) * 3.2;
    demoDrift.t = Math.max(-3, Math.min(3, demoDrift.t * 0.94));
    demoDrift.aw = Math.max(-0.05, Math.min(0.06, demoDrift.aw * 0.9));
    demoDrift.ppm = Math.max(-12, Math.min(18, demoDrift.ppm * 0.88));

    var linkRoll = Math.random();
    var link = linkRoll > 0.08 ? 'WIFI5G' : 'WIFI24G';

    ingest({
      t: +(demoBase.t + demoDrift.t).toFixed(2),
      aw: +(demoBase.aw + demoDrift.aw).toFixed(3),
      ppm: +(demoBase.ppm + demoDrift.ppm).toFixed(1),
      bat: +(demoBase.bat - (demoSeq * 0.0015)).toFixed(2),
      cur: +(Math.max(0.05, 0.25 + Math.random() * 0.08)).toFixed(3),
      pwr: +(Math.max(0.5, 24.2 * (0.25 + Math.random() * 0.08))).toFixed(2),
      fw: '1.0.4',
      lvl: Math.max(15, 92 - Math.floor(demoSeq / 12)),
      rssi: -48 - Math.floor(Math.random() * 14) + (link === 'WIFI5G' ? 8 : 0),
      link: link,
      seq: demoSeq++,
      addr: 1
    });
  }

  function toggleDemo() {
    var on = S.mode !== 'demo';
    if (on) {
      S.mode = 'demo';
      window.bridge.post({ cmd: 'demo', on: true });   // 请求宿主注入模拟数据
      appendLog('演示模式已开启');
      toast('演示模式已开启', 'info');
      startDemoWatch();
    } else {
      S.mode = 'idle';
      window.bridge.post({ cmd: 'demo', on: false });
      stopLocalDemo(); stopDemoWatch();
      appendLog('演示模式已关闭');
      toast('演示模式已关闭', 'info');
    }
    applyConnUI();
    updateSilent();
    updateStatus();
  }

  /* 无宿主（直接双击 html）时自动进入离线演示兜底 */
  function hostProbe() {
    if (hasHost()) return;
    if (S.mode === 'demo') return;
    S.mode = 'demo';
    window.bridge.post({ cmd: 'demo', on: true });
    appendLog('未检测到宿主程序，自动进入离线演示模式');
    applyConnUI();
    updateSilent();
    startLocalDemo();
  }

  /* ---------------- 设备匹配动作 ---------------- */
  function doScan() {
    if (S.scanning) return;
    S.scanning = true;
    updateScanUI();
    showError('');
    window.bridge.post({ cmd: 'scan' });
    appendLog('请求扫描设备…');
    // 无宿主时给一个明确的失败反馈
    if (!hasHost()) {
      setTimeout(function () {
        if (!hasHost() && S.scanning) {
          S.scanning = false;
          updateScanUI();
          showError('未检测到宿主程序，无法扫描真实设备');
        }
      }, 1500);
    }
  }

  function doConnect(ip, port, id) {
    ip = String(ip == null ? '' : ip).trim();
    if (!ip) { showError('请输入设备 IP 地址'); toast('请输入设备 IP 地址', 'err'); return; }
    S.conn = { state: 'connecting', name: '', ip: ip, port: (port == null ? '' : port), msg: '' };
    var dev = findDeviceByIp(ip);
    S.connectedId = (id != null && id !== '') ? String(id) : (dev ? dev.id : null);
    applyConnUI();
    applyDeviceStates();
    updateSilent();
    showError('');
    window.bridge.post({ cmd: 'connect', ip: ip, port: (port == null ? null : port), id: (id == null ? null : id) });
    appendLog('正在连接 ' + ip + (port ? (':' + port) : ''));
    if (!hasHost()) {
      setTimeout(function () {
        if (!hasHost() && S.conn.state === 'connecting') {
          S.conn.state = 'error';
          S.conn.msg = '未检测到宿主程序';
          applyConnUI(); applyDeviceStates(); updateSilent();
          showError('未检测到宿主程序，无法建立连接');
        }
      }, 2000);
    }
  }

  function doDisconnect() {
    window.bridge.post({ cmd: 'disconnect' });
    S.conn = { state: 'idle', name: '', ip: '', port: '', msg: '' };
    S.connectedId = null;
    applyConnUI();
    applyDeviceStates();
    updateSilent();
    appendLog('已请求断开连接');
    toast('已断开连接', 'info');
  }

  /* ---------------- 有线配网（USB 串口） ---------------- */
  function provState() {
    if (!S.prov) S.prov = { ports: [], port: '', opened: false, busy: '', nets: [] };
    return S.prov;
  }

  function provStatus(text, cls) {
    var el = $('provStatus');
    if (!el) return;
    el.textContent = text;
    el.className = 'prov-status' + (cls ? ' ' + cls : '');
  }

  function renderProv() {
    var p = provState();
    var busy = !!p.busy;
    var portSel = $('provPort');
    if (portSel) {
      var cur = portSel.value;
      portSel.innerHTML = '';
      if (!p.ports.length) {
        portSel.innerHTML = '<option value="">（未发现串口，请点刷新）</option>';
      } else {
        p.ports.forEach(function (pt) {
          var o = document.createElement('option');
          o.value = pt.port;
          o.textContent = pt.port + (pt.auto ? '（设备串口）' : '') + (pt.desc ? ' — ' + pt.desc : '');
          portSel.appendChild(o);
        });
        var want = p.port || cur;
        if (!p.ports.some(function (pt) { return pt.port === want; })) {
          var auto = p.ports.filter(function (pt) { return pt.auto; });
          want = (auto.length ? auto[0] : p.ports[0]).port;
        }
        portSel.value = want;
        p.port = want;
      }
      portSel.disabled = busy || p.opened;
    }
    if ($('btnProvPorts')) $('btnProvPorts').disabled = busy || p.opened;
    if ($('btnProvOpen')) {
      $('btnProvOpen').disabled = busy || (!p.opened && !p.port);
      $('btnProvOpen').textContent = p.opened ? '断开连接' : '连接设备';
    }
    var netSel = $('provNet');
    if (netSel) {
      netSel.innerHTML = '';
      if (!p.nets.length) {
        netSel.innerHTML = '<option value="">（点击「扫描网络」）</option>';
      } else {
        netSel.appendChild(new Option('（选择 WiFi）', ''));
        p.nets.forEach(function (n) {
          var label = n.s + '（' + n.r + 'dBm' + (n.e ? '，加密' : '，开放') + '）';
          netSel.appendChild(new Option(label, n.s));
        });
      }
      netSel.disabled = busy || !p.opened;
    }
    var canEdit = p.opened && !busy;
    if ($('btnProvScan')) $('btnProvScan').disabled = !canEdit;
    if ($('provSsid')) $('provSsid').disabled = !canEdit;
    if ($('provPass')) $('provPass').disabled = !canEdit;
    if ($('btnProvSet')) $('btnProvSet').disabled = !canEdit;
  }

  function applyProvState(d) {
    d = d || {};
    var p = provState();
    var stage = d.stage || '';
    p.busy = '';
    if (stage === 'ports') {
      p.ports = Array.isArray(d.ports) ? d.ports : [];
      renderProv();
      return;
    }
    if (stage === 'hello') {
      if (d.ok) {
        p.opened = true;
        provStatus('连接成功：' + (d.dev || '') + '  MAC ' + (d.mac || '') + '  固件 v' + (d.fw || '?') +
          (d.connected ? ('  当前已连 ' + d.ssid + '（' + (d.ip || '') + '）') : '  设备当前未连 WiFi'), 'ok');
      } else {
        p.opened = false;
        provStatus('连接失败：' + (d.err === 'no_reply' ? '设备无应答（确认串口选择是否正确）'
          : (d.err === 'no_port' ? '未选择串口' : (d.err || '未知'))), 'err');
      }
      renderProv();
      return;
    }
    if (stage === 'scan') {
      if (d.ok) {
        p.nets = d.nets || [];
        provStatus('扫描到 ' + p.nets.length + ' 个网络，请从下拉框选择或手动输入 WiFi 名称', 'ok');
      } else {
        provStatus('扫描失败：' + (d.err || '未知'), 'err');
      }
      renderProv();
      return;
    }
    if (stage === 'set') {
      if (d.ok) {
        provStatus('配对成功！设备已写入凭据并自动重启，IP：' + (d.ip || '?') +
          '。约几秒后可点「扫描设备」发现它。', 'ok');
      } else {
        provStatus('配对失败：' + (d.err === 'connect_failed' ? '连不上该 WiFi（检查密码/靠近热点）'
          : (d.err || '未知')) + '，可重试', 'err');
      }
      renderProv();
      return;
    }
    if (stage === 'closed') {
      p.opened = false;
      p.nets = [];
      provStatus('串口已断开。用 USB 线连接采集端后，选择串口并点击「连接设备」');
      renderProv();
      return;
    }
    if (stage === 'error') {
      p.opened = false;
      provStatus('错误：' + (d.msg || '未知'), 'err');
      renderProv();
    }
  }

  function doProvOpen() {
    var p = provState();
    if (p.opened) {                      // 再次点击 = 断开
      window.bridge.post({ cmd: 'prov_close' });
      return;
    }
    if (!p.port) { provStatus('请先选择串口', 'err'); return; }
    p.busy = 'open';
    renderProv();
    provStatus('正在连接 ' + p.port + '（921600）…');
    window.bridge.post({ cmd: 'prov_open', port: p.port });
  }

  function doProvScan() {
    var p = provState();
    if (!p.opened || p.busy) return;
    p.busy = 'scan';
    renderProv();
    provStatus('设备扫描 WiFi 中（约 5~15 秒）…');
    window.bridge.post({ cmd: 'prov_scan' });
  }

  function doProvSet() {
    var p = provState();
    if (!p.opened || p.busy) return;
    var ssid = $('provSsid').value.trim();
    var pass = $('provPass').value;
    if (!ssid) { provStatus('请选择或输入 WiFi 名称', 'err'); return; }
    p.busy = 'set';
    renderProv();
    provStatus('正在下发并连接 ' + ssid + '（最长约 30 秒）…');
    window.bridge.post({ cmd: 'prov_set', ssid: ssid, pass: pass });
  }


  /* ---------------- 设置 ---------------- */
  function loadSet() {
    try {
      var s = JSON.parse(localStorage.getItem('oilmoist_set') || '{}');
      Object.keys(SET).forEach(function (k) { if (s[k] !== undefined) SET[k] = s[k]; });
    } catch (e) { /* 忽略本地存储异常 */ }
    bindSetUI();
  }
  function saveSet() {
    try { localStorage.setItem('oilmoist_set', JSON.stringify(SET)); } catch (e) { /* 忽略 */ }
  }
  function bindSetUI() {
    $('setRefresh').value = SET.refresh;
    $('setWindow').value = SET.window;
    $('setTempMax').value = SET.tempMax; $('rTempMax').value = SET.tempMax;
    $('setAwMax').value = SET.awMax; $('rAwMax').value = SET.awMax;
    $('setPpmMax').value = SET.ppmMax; $('rPpmMax').value = SET.ppmMax;
    $('setAutoRec').checked = SET.autoRec;
    $('setSound').checked = SET.sound;
    $('setHistMax').value = SET.histMax;
    $('setCsvSplit').value = SET.csvSplitMinutes || 0;
  }
  function collectSet() {
    SET.refresh = Math.max(200, parseInt($('setRefresh').value, 10) || 1000);
    SET.window = Math.max(30, parseInt($('setWindow').value, 10) || 180);
    SET.tempMax = parseFloat($('setTempMax').value) || 80;
    SET.awMax = parseFloat($('setAwMax').value) || 0.8;
    SET.ppmMax = parseFloat($('setPpmMax').value) || 500;
    SET.autoRec = $('setAutoRec').checked;
    SET.sound = $('setSound').checked;
    SET.histMax = Math.max(100, parseInt($('setHistMax').value, 10) || 20000);
    SET.csvSplitMinutes = Math.max(0, parseInt($('setCsvSplit').value, 10) || 0);
  }

  /* ---------------- 标签页 ---------------- */
  function moveInk(btn) {
    var ink = $('tabInk');
    if (!ink || !btn || !btn.offsetParent) return;
    ink.style.width = btn.offsetWidth + 'px';
    ink.style.transform = 'translateX(' + (btn.offsetLeft - btn.parentElement.scrollLeft) + 'px)';
  }
  function switchTab(name) {
    var btns = document.querySelectorAll('.tab');
    Array.prototype.forEach.call(btns, function (b) {
      var on = b.dataset.tab === name;
      b.classList.toggle('active', on);
      if (on) moveInk(b);
    });
    var panels = document.querySelectorAll('.panel');
    Array.prototype.forEach.call(panels, function (p) { p.classList.toggle('active', p.id === 'tab-' + name); });
    if (name === 'monitor') setTimeout(function () {
      if (chart) chart.resize();
      if (eisChart) eisChart.resize();
    }, 60);
    if (name === 'match') { updateScanUI(); applyDeviceStates(); }
  }

  /* ---------------- 初始化 ---------------- */
  function init() {
    loadSet();
    initChart();
    initEisChart();
    updateEval();

    // 阻抗谱：博德图 / Nyquist 图切换 + 导入按钮（弹宿主目录选择框）
    Array.prototype.forEach.call(document.querySelectorAll('#segEisView button'), function (b) {
      b.addEventListener('click', function () {
        Array.prototype.forEach.call(document.querySelectorAll('#segEisView button'), function (x) {
          x.classList.remove('on');
        });
        b.classList.add('on');
        S.eis.view = b.dataset.v;
        if (window.__app.eis) window.__app.eis.view = S.eis.view;
        renderEis();
      });
    });
    $('btnImportEis').addEventListener('click', function () {
      window.bridge.post({ cmd: 'import_eis' });
    });
    // EIS 频率区间：全频 / 0.01–1Hz / 0.01–0.1Hz
    Array.prototype.forEach.call(document.querySelectorAll('#segEisRange button'), function (b) {
      b.addEventListener('click', function () {
        Array.prototype.forEach.call(document.querySelectorAll('#segEisRange button'), function (x) {
          x.classList.remove('on');
        });
        b.classList.add('on');
        S.eis.range = b.dataset.r;
        renderEis();
      });
    });
    // 介损/酸值卡片：均值 ⇄ 逐组（两张卡同步切换）
    Array.prototype.forEach.call(document.querySelectorAll('#segEvalMode button'), function (b) {
      b.addEventListener('click', function () {
        Array.prototype.forEach.call(document.querySelectorAll('#segEvalMode button'), function (x) {
          x.classList.remove('on');
        });
        b.classList.add('on');
        var per = b.dataset.m === 'per';
        $('cardTand').classList.toggle('per', per);
        $('cardAcid').classList.toggle('per', per);
        S.eisEval.mode = per ? 'per' : 'mean';
      });
    });

    // seg 曲线勾选（默认仅含水量，点击可叠加温度 / 水活性）
    var segBtns = document.querySelectorAll('#segMetrics button');
    Array.prototype.forEach.call(segBtns, function (b) {
      b.addEventListener('click', function () {
        b.classList.toggle('on');
        if (!document.querySelector('#segMetrics button.on')) b.classList.add('on');
        syncSeries();
      });
    });

    // 标签页
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
      b.addEventListener('click', function () { switchTab(b.dataset.tab); });
    });

    // 顶栏
    $('btnMatchGo').addEventListener('click', function () { switchTab('match'); });
    $('btnPause').addEventListener('click', togglePause);
    if ($('btnDemo')) $('btnDemo').addEventListener('click', toggleDemo);

    // 设备匹配页
    $('btnScan').addEventListener('click', doScan);
    $('btnManualConnect').addEventListener('click', function () {
      var p = parseInt($('manualPort').value, 10);
      doConnect($('manualIp').value, isFinite(p) ? p : null, null);
    });
    $('btnDisconnect').addEventListener('click', doDisconnect);

    // 有线配网（USB 串口）
    if ($('provPort')) $('provPort').addEventListener('change', function () { provState().port = this.value; });
    if ($('btnProvPorts')) $('btnProvPorts').addEventListener('click', function () {
      window.bridge.post({ cmd: 'prov_ports' });
      provStatus('正在枚举串口…');
    });
    if ($('btnProvOpen')) $('btnProvOpen').addEventListener('click', doProvOpen);
    if ($('btnProvScan')) $('btnProvScan').addEventListener('click', doProvScan);
    if ($('provNet')) $('provNet').addEventListener('change', function () {
      if (this.value) $('provSsid').value = this.value;
    });
    if ($('btnProvSet')) $('btnProvSet').addEventListener('click', doProvSet);
    renderProv();
    window.bridge.post({ cmd: 'prov_ports' });   // 启动即枚举串口（无宿主时命令留队列无害）
    // 固件升级按钮（触发宿主弹文件对话框 + POST http://<ip>/update）
    var _doOtaUpgrade = function () {
      var ip = (S.conn.state === 'connected') ? S.conn.ip : '';
      if (!ip && S.connectedId) {
        for (var i = 0; i < S.devices.length; i++) {
          if (S.devices[i].id === S.connectedId) { ip = S.devices[i].ip; break; }
        }
      }
      if (!ip) { toast('请先连接设备再升级固件', 'err'); return; }
      if (!confirm('确定要升级设备 %s (%s) 的固件吗？升级过程中请勿断电。'
                   .replace('%s', S.conn.name || S.connectedId || '设备').replace('%s', ip))) return;
      window.bridge.post({ cmd: 'ota_upgrade', ip: ip });
      toast('正在上传固件，请稍候…', 'info');
    };
    $('btnDevUpgrade').addEventListener('click', _doOtaUpgrade);
    $('devList').addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.di-connect') : null;
      if (!btn) return;
      var id = btn.getAttribute('data-id');
      var dev = null;
      for (var i = 0; i < S.devices.length; i++) { if (S.devices[i].id === id) { dev = S.devices[i]; break; } }
      if (dev) doConnect(dev.ip, dev.port, dev.id);
    });
    $('btnClearLog').addEventListener('click', function () {
      logs = []; renderLogs();
    });

    // 历史
    $('btnExport').addEventListener('click', exportCSV);
    $('btnClearHist').addEventListener('click', function () {
      S.hist = [];
      S.rawHist = [];
      var body = $('histBody');
      body.innerHTML = '<tr class="empty-row"><td colspan="11">暂无记录，连接设备后自动采集</td></tr>';
      var rbody = $('rawBody');
      if (rbody) rbody.innerHTML = '<tr class="empty-row"><td colspan="5">暂无原始帧，连接设备后自动采集</td></tr>';
      updateHistCount();
      toast('历史记录已清空', 'info');
    });
    // 历史双模式切换（正常数据 / 原始收发数据）
    Array.prototype.forEach.call(document.querySelectorAll('#histModeSeg button'), function (b) {
      b.addEventListener('click', function () { switchHistMode(b.dataset.hmode); });
    });

    // 设置：滑块与输入框联动
    $('rTempMax').addEventListener('input', function () { $('setTempMax').value = this.value; });
    $('rAwMax').addEventListener('input', function () { $('setAwMax').value = this.value; });
    $('rPpmMax').addEventListener('input', function () { $('setPpmMax').value = this.value; });
    $('setTempMax').addEventListener('input', function () { $('rTempMax').value = this.value; });
    $('setAwMax').addEventListener('input', function () { $('rAwMax').value = this.value; });
    $('setPpmMax').addEventListener('input', function () { $('rPpmMax').value = this.value; });

    // 启动时自动全屏
    $('setAutoFullscreen').addEventListener('change', function () {
      window.bridge.post({ cmd: 'set_autofullscreen', on: this.checked });
    });
    $('btnFullscreenNow').addEventListener('click', function () {
      window.bridge.post({ cmd: 'toggle_fullscreen' });
    });

    $('btnSaveSet').addEventListener('click', function () {
      collectSet(); saveSet();
      // 保存设置时同步到宿主，持久化到 config.json
      window.bridge.post({ cmd: 'set_autofullscreen', on: $('setAutoFullscreen').checked });
      window.bridge.post({ cmd: 'set_csv_split', minutes: SET.csvSplitMinutes });
      window.bridge.post({ cmd: 'set_max_records', count: SET.histMax });
      if (S.localDemo) { stopLocalDemo(); startLocalDemo(); }   // 应用新的刷新周期
      updateAlarm(); updateStatus();
      toast('设置已保存', 'ok');
    });
    $('btnResetSet').addEventListener('click', function () {
      SET = { refresh: 1000, window: 180, tempMax: 80, awMax: 0.80, ppmMax: 500,
              autoRec: true, sound: false, histMax: 20000, csvSplitMinutes: 0 };
      bindSetUI(); saveSet();
      window.bridge.post({ cmd: 'set_csv_split', minutes: 0 });
      window.bridge.post({ cmd: 'set_max_records', count: 20000 });
      if (S.localDemo) { stopLocalDemo(); startLocalDemo(); }
      updateAlarm();
      toast('已恢复默认设置', 'info');
    });

    renderLogs();
    renderDevices();
    updateTransportUI();
    updateScanUI();
    applyConnUI();
    updateStatus();
    updateSilent();

    setTimeout(function () { moveInk(document.querySelector('.tab.active')); }, 80);

    // DOM 就绪，执行之前暂存的宿主调用
    _ready = true;
    var q = _pending.slice(0); _pending.length = 0;
    q.forEach(function (fn) { try { fn(); } catch (e) { /* 忽略 */ } });

    // 数据停滞看门狗（1s 一拍）
    setInterval(watchdogTick, 1000);

    // 2 秒后仍未检测到宿主 → 自动进入离线演示，便于直接双击 html 预览
    setTimeout(hostProbe, 2000);
  }

  // 自检句柄：暴露 buildCSV 便于回归测试直接校验导出格式（不改变正常导出路径）
  window.__app.buildCSV = buildCSV;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
