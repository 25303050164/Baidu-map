import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Store } from './Store';
import App from './App';
import AlgorithmApp from './AlgorithmApp';
import CheckupApp from './checkup/CheckupApp';
import './global.css';
// `checkup` 单独成页：v2 的任务、修订与图层语义跟旧分析页不是一套，把它并进算法切换里
// 会让人以为那是同一个任务的两种算法，而它们连请求体都不一样。
const mode = import.meta.env.VITE_ANALYSIS_MODE;
const page = mode === 'demo' ? <Store><App/></Store>
  : mode === 'checkup' ? <CheckupApp/>
  : <AlgorithmApp/>;
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#147d70',colorInfo:'#147d70',fontFamily:'Inter, "Microsoft YaHei", "PingFang SC", sans-serif',borderRadius:7,controlHeight:36,colorText:'#233b48',fontSize:13}}}>{page}</ConfigProvider></React.StrictMode>);
