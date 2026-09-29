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
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#147d70',colorInfo:'#147d70',fontFamily:'Inter, "Microsoft YaHei", "PingFang SC", sans-serif',borderRadius:7,controlHeight:36,colorText:'#233b48',fontSize:13}}}>{page}</ConfigProvider></React.StrictMode>);
