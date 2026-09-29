import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Store } from './Store';
import App from './App';
import AlgorithmApp from './AlgorithmApp';
import './global.css';
// 除了离线演示，所有模式都进入体检 v2 工作台，可切换 E8.2 与 OSM＋百度算法。
const mode = import.meta.env.VITE_ANALYSIS_MODE;
const page = mode === 'demo' ? <Store><App/></Store> : <AlgorithmApp/>;
// 主题与 global.css 的变量同一套：暖白纸面、墨色字、细线；圆角收小，控件略紧凑。
const theme = {
  token: {
    colorPrimary: '#147d70', colorInfo: '#147d70', colorWarning: '#b54708', colorError: '#b42318',
    colorText: '#1d2327', colorTextSecondary: '#4b5459', colorTextTertiary: '#7d858a',
    colorBorder: '#d3cec1', colorBorderSecondary: '#e6e2d8', colorBgLayout: '#f3f1eb',
    fontFamily: '"Segoe UI", "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif',
    fontSize: 13, borderRadius: 4, borderRadiusLG: 6, controlHeight: 34, boxShadowSecondary: '0 6px 20px rgba(29,35,39,.14)',
  },
  components: {
    Segmented: { itemSelectedBg: '#ffffff', trackBg: '#ebe8df', itemColor: '#4b5459' },
    Drawer: { colorBgElevated: '#fbfaf6' },
  },
};
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={theme} button={{ autoInsertSpace: false }}>{page}</ConfigProvider></React.StrictMode>);
