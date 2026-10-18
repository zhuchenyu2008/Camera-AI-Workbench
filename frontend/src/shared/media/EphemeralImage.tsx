import { useEffect, useState } from 'react';
import { checkedFetch, ApiError } from '../../api/client';
import { serverNow } from '../time';
import { useVisible } from '../hooks';

// Media is never stored in Query, localStorage, IndexedDB or a service worker.
// Every object URL has one owner and is revoked on expiry, 410 and teardown.
export function useEphemeralImage(url:string|null, {enabled=true,expiresAt,repeat=0}:{enabled?:boolean;expiresAt?:string;repeat?:number}={}) {
  const visible=useVisible();
  const [state,set]=useState<{url:string|null;capturedAt:string|null;error:unknown}>({url:null,capturedAt:null,error:null});
  const [epoch,setEpoch]=useState(0);
  useEffect(()=>{const reset=()=>setEpoch(n=>n+1);window.addEventListener('media-invalidated',reset);return()=>window.removeEventListener('media-invalidated',reset);},[]);
  useEffect(()=>{
    let object:string|null=null, disposed=false, timer:ReturnType<typeof setTimeout>|undefined, expiry:ReturnType<typeof setTimeout>|undefined;
    const controller=new AbortController();
    const clear=()=>{if(object){URL.revokeObjectURL(object);object=null;}if(!disposed)set(s=>({...s,url:null}));};
    set({url:null,capturedAt:null,error:null});
    const remaining=expiresAt?Date.parse(expiresAt)-serverNow():Infinity;
    if(!url||!enabled||!visible)return;
    const expire=()=>{clear();controller.abort();clearTimeout(timer);set(s=>({...s,error:new ApiError(410,'EVIDENCE_EXPIRED','原图已过期；文字记录仍可查看')}));};
    if(remaining<=0){expire();return;}
    if(Number.isFinite(remaining))expiry=setTimeout(expire,remaining);
    const fetchImage=async()=>{
      try {
        const response=await checkedFetch(url,{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(15000)])});
        if(!response.headers.get('Content-Type')?.startsWith('image/'))throw new ApiError(502,'INVALID_IMAGE','媒体接口未返回图片');
        const blob=await response.blob();
        if(disposed||controller.signal.aborted)return;
        clear();object=URL.createObjectURL(blob);set({url:object,capturedAt:response.headers.get('X-Captured-At'),error:null});
      } catch(error) {
        if(disposed||controller.signal.aborted)return;
        clear();set({url:null,capturedAt:null,error});
        if(error instanceof ApiError&&[401,410].includes(error.status)){controller.abort();return;}
      }
      if(repeat&&!disposed&&!controller.signal.aborted)timer=setTimeout(fetchImage,repeat);
    };
    void fetchImage();
    return()=>{disposed=true;controller.abort();clearTimeout(timer);clearTimeout(expiry);if(object)URL.revokeObjectURL(object);};
  },[url,enabled,visible,expiresAt,repeat,epoch]);
  return state;
}
export function ProtectedImage({url,alt,className='',onLoad}:{url:string|null;alt:string;className?:string;onLoad?:(image:HTMLImageElement)=>void}) {
  const image=useEphemeralImage(url);
  return image.url?<img className={className} src={image.url} alt={alt} loading="lazy" onLoad={e=>onLoad?.(e.currentTarget)}/>:<span className="image-placeholder">{image.error?'图片不可用':'正在读取图片'}</span>;
}
