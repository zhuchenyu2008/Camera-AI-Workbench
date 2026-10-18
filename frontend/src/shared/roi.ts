export type Point=[number,number];
export function containedRect(box:{width:number;height:number},image:{width:number;height:number}) {
  const scale=Math.min(box.width/image.width,box.height/image.height);
  const width=image.width*scale,height=image.height*scale;
  return {x:(box.width-width)/2,y:(box.height-height)/2,width,height};
}
export function normalizePoint(x:number,y:number,rect:{x:number;y:number;width:number;height:number}):Point|null {
  if(rect.width<=0||rect.height<=0||x<rect.x||y<rect.y||x>rect.x+rect.width||y>rect.y+rect.height)return null;
  return [(x-rect.x)/rect.width,(y-rect.y)/rect.height];
}
export function polygonError(points:Point[]) {
  if(points.length<3||points.length>32)return '多边形须为 3–32 个顶点';
  if(points.some(p=>p.some(n=>!Number.isFinite(n)||n<0||n>1)))return '区域坐标超出画面';
  const cross=(a:Point,b:Point,c:Point)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
  const area=Math.abs(points.reduce((s,p,i)=>s+p[0]*points[(i+1)%points.length][1]-points[(i+1)%points.length][0]*p[1],0));
  if(area<1e-8)return '区域必须有有效面积';
  if(new Set(points.map(p=>p.join(','))).size!==points.length)return '区域顶点不能重复';
  for(let i=0;i<points.length;i++)for(let j=i+1;j<points.length;j++){
    if(j===i+1||(i===0&&j===points.length-1))continue;
    const a=points[i],b=points[(i+1)%points.length],c=points[j],d=points[(j+1)%points.length];
    if(cross(a,b,c)*cross(a,b,d)<=0&&cross(c,d,a)*cross(c,d,b)<=0)return '多边形不能自交';
  }
  return null;
}
