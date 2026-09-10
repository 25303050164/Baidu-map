import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Store } from './Store';
import App from './App';
import './global.css';
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={zhCN} theme={{token:{colorPrimary:'#147d70',colorInfo:'#147d70',fontFamily:'Inter, "Microsoft YaHei", "PingFang SC", sans-serif',borderRadius:7,controlHeight:36,colorText:'#233b48',fontSize:13}}}><Store><App/></Store></ConfigProvider></React.StrictMode>);
