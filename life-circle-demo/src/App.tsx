import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Checkbox, Drawer, Input, Segmented, Tag, Tooltip } from 'antd';
import { ArrowRightOutlined, CheckCircleOutlined, CompassOutlined, EnvironmentOutlined, ExperimentOutlined, FileTextOutlined, InfoCircleOutlined, BlockOutlined, ReloadOutlined, SettingOutlined, ShopOutlined, MedicineBoxOutlined, ReadOutlined, RadarChartOutlined, CloseOutlined } from '@ant-design/icons';
import { useStore } from './Store';
import { categories, categoryMeta, scenarioLabels, type Category, type Filter, type Scenario } from './types';
import { filterFacilities, findSample, summarize } from './domain';
import { analysisResultToReportView } from './report';
import { ReportPage } from './ReportPage';
import { AnalysisLoading } from './components/AnalysisLoading';
import { loadingOutcome } from './components/loadingFlow';
import { DemoMap, type Focus } from './Map';
import { FacilityChart } from './Chart';
import styles from './styles.module.css';
const icons: Record<Category, React.ReactNode> = { market: <ShopOutlined/>, pharmacy: <MedicineBoxOutlined/>, school: <ReadOutlined/> };
export default function App() {
  const {state,dispatch,analyze}=useStore();
  const [phone,setPhone]=useState(()=>window.matchMedia('(max-width: 800px)').matches);
  useEffect(()=>{const media=window.matchMedia('(max-width: 800px)');const change=()=>{setPhone(media.matches);if(!media.matches)setMobilePanel(null);};media.addEventListener('change',change);return()=>media.removeEventListener('change',change);},[]);
  const [report,setReport]=useState(false),[mobilePanel,setMobilePanel]=useState<'results'|null>(null),[tab,setTab]=useState<string>('问题区域');
  // AI 体检 Loading：点击开始体检后立即显示；分析成功且动画播完→关闭并自动打开报告；失败→立即关闭，错误横幅按原逻辑接管。
  const [analysisLoading,setAnalysisLoading]=useState(false),[animationDone,setAnimationDone]=useState(false);
  const [focus,setFocus]=useState<Focus>(),[layers,setLayers]=useState({circle:true,facilities:true,blind:true});
  const [coords,setCoords]=useState({lng:String(state.center.lng),lat:String(state.center.lat)}),[coordError,setCoordError]=useState('');
  useEffect(()=>{setCoords({lng:String(state.center.lng),lat:String(state.center.lat)});setCoordError('');},[state.center]);
  useEffect(()=>{setFocus(undefined);},[state.result,state.filter,state.center]);
  const sample=findSample(state.samples,state.center),result=state.result,summary=result?summarize(result):undefined;
  const reportView=useMemo(()=>result?analysisResultToReportView(result):undefined,[result]);
  // 编排策略见 components/loadingFlow.ts：分析失败立即关闭；成功则等动画播完再关闭并自动展示报告。
  const loading=loadingOutcome({started:analysisLoading,animationDone,analysisStatus:state.status});
  useEffect(()=>{if(!analysisLoading||loading.visible)return;setAnalysisLoading(false);if(loading.openReport)setReport(true);},[analysisLoading,loading.visible,loading.openReport]);
  const pending=coords.lng!==String(state.center.lng)||coords.lat!==String(state.center.lat);
  function applyCoords(){ const lng=Number(coords.lng),lat=Number(coords.lat);if(!coords.lng.trim()||!coords.lat.trim()||!Number.isFinite(lng)||!Number.isFinite(lat)||lng< -180||lng>180||lat< -90||lat>90){setCoordError('请输入有效经纬度：经度 -180～180，纬度 -90～90。');return;}dispatch({type:'edit',center:{lng,lat}});setCoordError(''); }
  const analysisPanel=<>
    <div className={styles.panelHeading}><div><span className={styles.eyebrow}>ANALYSIS</span><h2>分析设置</h2></div><SettingOutlined className={styles.muted}/></div>
    <div className={styles.field}><label htmlFor="sample">演示样例</label><select id="sample" value={sample?.id??''} onChange={e=>{const chosen=state.samples.find(s=>s.id===e.target.value);if(chosen)dispatch({type:'edit',center:chosen.center});}}><option value="" disabled>自定义位置</option>{state.samples.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select><small>虚构街区，仅用于展示产品流程</small></div>
    <div className={styles.field}><label htmlFor="scenario">演示场景</label><select id="scenario" value={state.scenario} onChange={e=>dispatch({type:'edit',scenario:e.target.value as Scenario})}>{Object.entries(scenarioLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></div>
    <div className={styles.field}><div className={styles.rowBetween}><label>中心点坐标</label><span className={styles.coordinateBadge}>演示坐标</span></div><div className={styles.coordinateInputs}><div><span>经度 LNG</span><Input aria-label="中心点经度" value={coords.lng} onChange={e=>setCoords({...coords,lng:e.target.value})} onPressEnter={applyCoords}/></div><div><span>纬度 LAT</span><Input aria-label="中心点纬度" value={coords.lat} onChange={e=>setCoords({...coords,lat:e.target.value})} onPressEnter={applyCoords}/></div></div><Button size="small" block onClick={applyCoords} disabled={!pending}>应用坐标</Button>{coordError&&<p role="alert" className={styles.errorText}>{coordError}</p>}{pending&&<small>应用坐标后可开始体检</small>}</div>
    <div className={styles.rules}><div><span className={styles.ruleIcon}><CompassOutlined/></span><div><strong>15 分钟</strong><p>中心点步行可达范围</p></div><span className={styles.ruleUnit}>时间</span></div><div><span className={styles.ruleIcon}><RadarChartOutlined/></span><div><strong>1 公里</strong><p>分类服务盲区判断</p></div><span className={styles.ruleUnit}>距离</span></div><small>两套指标独立展示 · 规则仅作演示</small></div>
    <div className={styles.field}><label>关注的民生设施</label><div className={styles.categoryPills}>{categories.map(c=><span key={c} style={{color:categoryMeta[c].color}}>{icons[c]}{categoryMeta[c].label}</span>)}</div></div>
    <Button aria-label={state.status==='failed'?'重试分析':'开始体检'} type="primary" size="large" block icon={state.status==='failed'?<ReloadOutlined/>:<RadarChartOutlined/>} loading={state.status==='loading'} disabled={pending||!state.samples.length||analysisLoading} onClick={()=>{setMobilePanel(null);setAnalysisLoading(true);setAnimationDone(false);void analyze();}}>{state.status==='failed'?'重试分析':'开始体检'}</Button>
    <p className={styles.buttonHint}>无需 API 密钥 · 本地模拟分析</p>
    <div className={styles.layerSection}><h3><BlockOutlined/> 地图图层</h3>{([{key:'circle',label:'15 分钟范围',color:'#168875'},{key:'facilities',label:'民生设施',color:'#397ac6'},{key:'blind',label:'1 公里盲区',color:'#7f8e94'}] as const).map(l=><div className={styles.layerRow} key={l.key}><Checkbox checked={layers[l.key]} onChange={e=>setLayers({...layers,[l.key]:e.target.checked})}>{l.label}</Checkbox><span style={{background:l.color}}/></div>)}</div>
    <div className={styles.note}><InfoCircleOutlined/><p>这里展示的是产品交互效果。道路、设施与体检结论均为模拟数据。</p></div>
  </>;
  const resultPanel=<>
    <div className={styles.panelHeading}><div><span className={styles.eyebrow}>OVERVIEW</span><h2>生活圈概览</h2></div><Tag variant="filled" color={result?'success':'default'}>{result?'演示结果':'待体检'}</Tag></div>
    <div className={styles.totalCard}><span>{summary?.unknownCount?'圈内已记录设施':'圈内设施总数'}</span><div><strong data-testid="total-count">{summary?.total??'—'}</strong><span>处</span><span className={styles.totalIllustration}><ShopOutlined/></span></div><small>全部类别 · 15 分钟圈内 · 演示</small></div>
    <div className={styles.countCards}>{categories.map(c=><div key={c}><span style={{color:categoryMeta[c].color}}>{icons[c]}</span><strong data-testid={`count-${c}`}>{summary?(summary[c]??'—'):'—'}</strong><small>{categoryMeta[c].label}</small></div>)}</div>
    {result&&<div className={styles.chartWrap}><FacilityChart result={result}/></div>}
    <div className={styles.resultTabs}><Segmented block options={['问题区域','设施列表']} value={tab} onChange={setTab}/></div>
    <div className={styles.listArea}>
      {!result?<div className={styles.empty}><span className={styles.emptyIcon}><RadarChartOutlined/></span><h3>从一个中心点开始</h3><p>选择左侧演示样例并开始体检，<br/>探索设施分布与生活服务缺口。</p></div>:tab==='问题区域'?<>
        <div className={styles.listCaption}>1 公里服务情况 <span>{summary!.blindCount} 个盲区 · {summary!.unknownCount} 处待补充</span></div>
        {result.zones.filter(z=>state.filter==='all'||z.category===state.filter).map(z=><button key={z.id} className={`${styles.zoneCard} ${z.status==='unknown'?styles.unknownCard:''}`} onClick={()=>{setLayers(l=>({...l,blind:true}));setFocus({...z.position,id:z.id,name:z.name,detail:z.reason,nonce:Date.now()});setMobilePanel(null);}}><span className={styles.rowBetween}><strong>{z.name}</strong><span>{z.status==='blind'?'服务盲区':'无法判断'}</span></span><p>{categoryMeta[z.category].label} · {z.status==='blind'?'1 公里内缺失（演示）':'数据尚不完整'}</p><span className={styles.locateLink}><EnvironmentOutlined/> 在地图中查看 <ArrowRightOutlined/></span></button>)}
        {result.zones.filter(z=>state.filter==='all'||z.category===state.filter).length===0&&<div className={styles.healthy}><CheckCircleOutlined/><h3>{result.zones.length?'当前类别无问题记录':'未发现预设服务盲区'}</h3><p>仅对应当前演示场景，<br/>不代表真实社区的服务水平。</p></div>}
      </>:<><div className={styles.listCaption}>设施点位 <span>仅显示 15 分钟圈内</span></div>{filterFacilities(result,state.filter).map(f=><button key={f.id} className={styles.facilityRow} onClick={()=>{setLayers(l=>({...l,facilities:true}));setFocus({...f,detail:`${categoryMeta[f.category].label} · 15 分钟圈内（演示）`,nonce:Date.now()});setMobilePanel(null);}}><span className={styles.facilityIcon} style={{color:categoryMeta[f.category].color}}>{icons[f.category]}</span><span><strong>{f.name}</strong><small>{categoryMeta[f.category].label} · 圈内设施</small></span><EnvironmentOutlined/></button>)}</>}
    </div>
    <div className={styles.reportButton}><Button block icon={<FileTextOutlined/>} disabled={!result} onClick={()=>setReport(true)}>查看完整体检报告 <ArrowRightOutlined/></Button></div>
  </>;
  return <div className={styles.app}>
    <header className={styles.header}><div className={styles.brand}><span><EnvironmentOutlined/></span><strong>邻里<span>生活圈体检</span></strong></div><nav className={styles.nav}><span className={styles.activeNav}>生活圈分析</span><span>15 MINUTE CITY</span></nav><div className={styles.headerRight}><span className={styles.offlineDot}/>本地运行<Tag color="gold" variant="filled"><ExperimentOutlined/> 演示数据</Tag>{result&&reportView&&<Tooltip title={`最近报告：${reportView.centerName} · ${new Date(reportView.generatedAt).toLocaleString('zh-CN',{hour12:false})}`}><Button aria-label="查看体检报告" color="primary" variant="outlined" size="small" icon={<FileTextOutlined/>} onClick={()=>setReport(true)}>查看体检报告</Button></Tooltip>}</div></header>
    <section className={styles.intro}><div><div className={styles.eyebrow}>COMMUNITY INSIGHTS <span> / </span> 社区空间洞察</div><h1>看见身边的生活半径<span className={styles.titleDot}>.</span></h1><p>从一个地点出发，了解步行可达范围，发现生活服务缺口。</p></div><div className={styles.introRight}><div><span className={styles.liveDot}/>当前分析位置</div><strong>{sample?.name??'自定义位置'}</strong><small>青禾街区为虚构演示样例</small></div></section>
    <div className={styles.mobileToolbar}><Button aria-label="查看结果" icon={<RadarChartOutlined/>} onClick={()=>setMobilePanel('results')}>查看结果</Button></div>
    <main className={styles.workspace}>
      <aside className={styles.leftPanel}>{analysisPanel}</aside>
      <section className={styles.mapColumn}>
        <div className={styles.mapBar}><div className={styles.filters}>{(['all',...categories] as Filter[]).map(c=><button key={c} aria-pressed={state.filter===c} className={state.filter===c?styles.activeFilter:''} onClick={()=>dispatch({type:'filter',filter:c})}>{c==='all'?'全部设施':<><span style={{background:categoryMeta[c].color}}/>{categoryMeta[c].label}</>}</button>)}</div><TooltipNote/></div>
        {state.dirty&&<div className={styles.staleBanner} role="status">条件已修改，需重新分析。下方保留的是「{result?.sample.name} / {result&&scenarioLabels[result.scenario]}」旧结果。</div>}
        {state.error&&<Alert type={state.status==='unavailable'?'warning':'error'} title={state.error} showIcon className={styles.errorBanner}/>}
        <div className={styles.narrowNotice} role="status">窗口过窄，交互空间有限，请加宽窗口或横屏使用。</div>
        <DemoMap center={state.center} samples={state.samples} result={result} filter={state.filter} layers={layers} focus={focus} onFocus={setFocus} onPick={center=>dispatch({type:'edit',center})} loading={state.status==='loading'}/>
        <div className={styles.legend}><span><i className={styles.reachLegend}/>15 分钟范围</span><span><i className={styles.blindLegend}/>1 公里服务盲区</span><span><i className={styles.unknownLegend}/>无法判断</span><span className={styles.legendHint}>两套指标独立展示</span></div>
      </section>
      {!phone&&<aside className={styles.rightPanel}>{resultPanel}</aside>}
    </main>
    <footer className={styles.footer}><span><InfoCircleOutlined/> 本 Demo 仅展示交互效果，未接入真实地图与算法。</span><span>邻里 · 让社区生活触手可及</span></footer>
    <Drawer title="生活圈概览" open={mobilePanel==='results'} placement="right" onClose={()=>setMobilePanel(null)} size={340}>{resultPanel}</Drawer>
    <Drawer title="生活圈体检报告" open={report} onClose={()=>setReport(false)} size={680} extra={<Tag color="gold">演示数据</Tag>}>
      {reportView
        ?<ReportPage view={reportView} stale={state.dirty} lastAttemptFailed={state.status==='failed'||state.status==='unavailable'}/>
        :<div className={styles.empty}><span className={styles.emptyIcon}><RadarChartOutlined/></span><h3>尚无分析结果</h3><p>完成一次体检后，<br/>这里将生成完整报告。</p></div>}
    </Drawer>
    <AnalysisLoading visible={analysisLoading} onFinish={()=>setAnimationDone(true)}/>
  </div>;
}
function TooltipNote(){return <span className={styles.mapBarNote}><EnvironmentOutlined/> 点击地图选择中心点</span>;}


