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
// 离线演示沿用原主题：它的版式检查按原来的字号与控件高度写成。
const demoTheme = {
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
// 体检工作台与 global.css 的变量同一套：百度开放平台的主蓝、浅灰蓝底、白卡片与大圆角。
const workbenchTheme = {
  token: {
    colorPrimary: '#3366ff', colorInfo: '#3366ff', colorSuccess: '#12b886', colorWarning: '#ff8a00',
    colorError: '#f53f3f', colorLink: '#3366ff',
    colorText: '#333333', colorTextSecondary: '#666666', colorTextTertiary: '#999999',
    colorBorder: '#dcdfe6', colorBorderSecondary: '#e9ebf0', colorBgLayout: '#f5f7fa',
    fontFamily: '"PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif',
    fontSize: 14, borderRadius: 8, borderRadiusLG: 12, controlHeight: 36,
    boxShadowSecondary: '0 12px 36px rgba(120,140,200,.24)',
  },
  components: {
    Segmented: { itemSelectedBg: '#ffffff', itemSelectedColor: '#3366ff', trackBg: '#f0f2f5', itemColor: '#666666',
      itemHoverColor: '#3366ff' },
    Drawer: { colorBgElevated: '#ffffff' },
    Button: { primaryShadow: '0 4px 12px rgba(51,102,255,.24)', fontWeight: 500 },
    Checkbox: { borderRadiusSM: 4 },
  },
};
const theme = mode === 'demo' ? demoTheme : workbenchTheme;
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={theme} button={{ autoInsertSpace: false }}>{page}</ConfigProvider></React.StrictMode>);
