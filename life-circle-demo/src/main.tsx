import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Store } from './Store';
import App from './App';
import AlgorithmApp from './AlgorithmApp';
import './global.css';
// 除了离线演示，所有模式都进同一个主入口：页面（体检 v2 / 旧版成圈分析）× 算法（E8.2 /
// OSM＋百度）。v2 与旧版的任务互不相通，各有各的任务和结果，入口只负责切换与显示。
const mode = import.meta.env.VITE_ANALYSIS_MODE;
const page = mode === 'demo' ? <Store><App/></Store> : <AlgorithmApp/>;
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#147d70',colorInfo:'#147d70',fontFamily:'Inter, "Microsoft YaHei", "PingFang SC", sans-serif',borderRadius:7,controlHeight:36,colorText:'#233b48',fontSize:13}}}>{page}</ConfigProvider></React.StrictMode>);
