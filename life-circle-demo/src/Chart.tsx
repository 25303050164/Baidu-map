import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import { categories, categoryMeta, type AnalysisResult } from './types';
import { summarize } from './domain';
import { reportBarSeries, type ReportViewModel } from './report';
echarts.use([BarChart, GridComponent, TooltipComponent, SVGRenderer]);
export function FacilityChart({ result }: { result: AnalysisResult }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(()=>{
    if(!ref.current) return;
    const chart=echarts.init(ref.current,undefined,{renderer:'svg'}), summary=summarize(result);
    chart.setOption({animation:false, grid:{left:52,right:24,top:8,bottom:20},xAxis:{type:'value',minInterval:1,axisLabel:{color:'#8b979f',fontSize:10},splitLine:{lineStyle:{color:'#eef1f3'}}},yAxis:{type:'category',data:categories.map(c=>categoryMeta[c].label),inverse:true,axisTick:{show:false},axisLine:{show:false},axisLabel:{color:'#667680',fontSize:11}},series:[{type:'bar',barWidth:10,data:categories.map(c=>({value:summary[c],itemStyle:{color:categoryMeta[c].color,borderRadius:[0,3,3,0]}}))}]});
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(ref.current);
    return ()=>{observer.disconnect();chart.dispose();};
  },[result]);
  return <div ref={ref} style={{height:140,width:'100%'}} role="img" aria-label={`圈内设施柱状图：${categories.map(c=>`${categoryMeta[c].label}${summarize(result)[c]??'数据不足'}`).join('，')}`}/>;
}

// 报告专用柱状图：只消费 ReportViewModel，不直接读取 AnalysisResult。
// 数据不足的类别 value 为 null，ECharts 不会绘制柱形（不会画成 0），说明文字由 Report 页面补充。
export function ReportBarChart({ view }: { view: ReportViewModel }) {
  const ref = useRef<HTMLDivElement>(null);
  const series = useMemo(() => reportBarSeries(view), [view]);
  useEffect(()=>{
    if(!ref.current) return;
    const chart=echarts.init(ref.current,undefined,{renderer:'svg'});
    chart.setOption({animation:false, grid:{left:96,right:24,top:8,bottom:20},xAxis:{type:'value',minInterval:1,axisLabel:{color:'#8b979f',fontSize:10},splitLine:{lineStyle:{color:'#eef1f3'}}},yAxis:{type:'category',data:series.map(s=>s.axisLabel),inverse:true,axisTick:{show:false},axisLine:{show:false},axisLabel:{color:'#667680',fontSize:11}},series:[{type:'bar',barWidth:10,data:series.map(s=>({value:s.value,itemStyle:{color:s.color,borderRadius:[0,3,3,0]}}))}]});
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(ref.current);
    return ()=>{observer.disconnect();chart.dispose();};
  },[series]);
  return <div ref={ref} style={{height:140,width:'100%'}} role="img" aria-label={`报告设施柱状图：${series.map(s=>`${s.label}${s.value??'数据不足'}`).join('，')}`}/>;
}
