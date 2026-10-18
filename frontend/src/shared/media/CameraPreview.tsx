import { useEffect, useRef, useState } from 'react';
import { CameraOff, Image as ImageIcon, Maximize2, Play, Video } from 'lucide-react';
import { api, sameOriginMedia } from '../../api/client';
import { useApi } from '../../api/query';
import { useVisible } from '../hooks';
import { Button, Badge, ErrorNotice, ScopeLink } from '../components';
import { captureLabel } from '../labels';
import { clock, serverNow } from '../time';
import type { CameraDto, PreviewDto } from '../../api/generated/types';
import { useEphemeralImage } from './EphemeralImage';

export function CameraPreview({camera}:{camera:CameraDto}) {
  const visible=useVisible(),[forceSnapshot,setForce]=useState(false),[failed,setFailed]=useState(false);
  const query=useApi('getPreviewStatus',{path:{camera_id:camera.id}},{enabled:visible,poll:5000});
  useEffect(()=>{setForce(false);setFailed(false);},[camera.id]);
  const preview=query.data?.data, mode=forceSnapshot||failed?'snapshot':preview?.mode;
  const active=visible&&camera.runtime.capture_state==='online'&&preview?.mode!=='unavailable';
  return <div className="preview-component"><div className="preview-stage">{active&&preview?mode==='hls'?<HlsPlayer key={preview.session_id} preview={preview} onFallback={()=>setFailed(true)}/>:<Snapshot cameraId={camera.id} preview={preview}/>:<div className="preview-empty"><CameraOff size={38} strokeWidth={1.3}/><strong>{visible?captureLabel(camera.runtime):'页面已隐藏，预览已暂停'}</strong><p>预览不会主动启动采集</p><ScopeLink className="button dark-secondary" to={`/cameras/${camera.id}`}>查看机位与运行计划</ScopeLink></div>}<div className="preview-top"><Badge tone={active?'live':'dark'} dot={active}>{active?(mode==='snapshot'?'每秒快照':'HLS 预览'):'最后状态'}</Badge><span className="camera-name"><Video size={14}/>{camera.name}</span></div></div><div className="preview-bottom"><div><strong>{camera.name}</strong><span className="caption muted">画面机位独立于统计范围</span></div><div className="actions"><Button variant="ghost" onClick={()=>setForce(v=>!v)} disabled={!preview||preview.mode==='unavailable'}><ImageIcon size={16}/>{forceSnapshot?'恢复自动预览':'使用快照'}</Button></div></div>{preview?.mode==='hls'&&!forceSnapshot&&!failed&&<p className="caption muted">HLS 目标延迟约 12–30 秒；并非毫秒级同步。</p>}{failed&&<p className="caption">当前编码或播放器不可用，已退回每秒快照。</p>}<ErrorNotice error={query.error} retry={()=>void query.refetch()}/></div>;
}
function Snapshot({cameraId,preview}:{cameraId:string;preview:PreviewDto}) {
  const image=useEphemeralImage(preview.snapshot_url??`/api/cameras/${encodeURIComponent(cameraId)}/snapshot`,{repeat:1000});
  const captured=image.capturedAt??preview.captured_at, stale=!captured||serverNow()-Date.parse(captured)>preview.stale_after_ms;
  return <>{image.url?<img className="preview-image" src={image.url} alt="当前选定机位保存的最新快照"/>:<div className="preview-empty"><ImageIcon size={32}/><p>{image.error?'快照暂不可用':'正在读取最新画面'}</p></div>}<div className="frame-time"><span className={stale?'stale-dot':'status-dot'}/>{stale?'画面已停止更新':'最近画面'} · {clock(captured,true)} 北京时间</div>{image.error&&<div className="preview-error"><ErrorNotice error={image.error}/></div>}</>;
}
function HlsPlayer({preview,onFallback}:{preview:PreviewDto;onFallback:()=>void}) {
  const ref=useRef<HTMLVideoElement>(null),[needsPlay,setNeedsPlay]=useState(false);
  useEffect(()=>{
    const video=ref.current!;let disposed=false, hls:{destroy:()=>void;startLoad:()=>void;recoverMediaError:()=>void}|undefined,recoveries=0;
    const play=()=>{void video.play().catch(()=>{if(!disposed)setNeedsPlay(true);});};
    const error=async()=>{try{await api('getSession');await api('getPreviewStatus',{path:{camera_id:preview.camera_id}});if(!disposed)onFallback();}catch{/* 401 is handled globally. */}};
    const setup=async()=>{
      if(!preview.manifest_url){onFallback();return;}
      const url=sameOriginMedia(preview.manifest_url);
      if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=url;video.addEventListener('error',error);play();return;}
      const {default:Hls}=await import('hls.js');if(disposed)return;
      if(!Hls.isSupported()){onFallback();return;}
      const instance=new Hls({maxBufferLength:20,backBufferLength:0,xhrSetup:xhr=>{xhr.withCredentials=true;},fetchSetup:(context,init)=>new Request(sameOriginMedia(context.url),{...init,credentials:'same-origin',cache:'no-store'})});hls=instance;
      instance.on(Hls.Events.MANIFEST_PARSED,play);
      instance.on(Hls.Events.ERROR,(_event,data)=>{if(!data.fatal)return;if(recoveries++<2){if(data.type===Hls.ErrorTypes.NETWORK_ERROR)instance.startLoad();else instance.recoverMediaError();}else{instance.destroy();void error();}});
      instance.loadSource(url);instance.attachMedia(video);
    };
    void setup().catch(()=>{if(!disposed)onFallback();});
    return()=>{disposed=true;video.removeEventListener('error',error);hls?.destroy();video.pause();video.removeAttribute('src');video.load();};
  },[preview.camera_id,preview.session_id,preview.manifest_url]);
  return <><video ref={ref} className="preview-image" playsInline muted controls aria-label="选定机位 HLS 预览"/>{needsPlay&&<Button className="play-button" onClick={()=>{void ref.current?.play();setNeedsPlay(false);}}><Play size={17}/>播放预览</Button>}<div className="frame-time">最近采集 · {clock(preview.captured_at,true)} 北京时间</div></>;
}
