import type { CameraRuntime, EventDto } from '../api/generated/types';
export const activities: Record<EventDto['activity'], string> = { reading:'阅读',writing:'书写',computer:'使用电脑',phone:'使用手机',away:'离开画面',resting:'休息',other:'其他活动',unknown:'无法判断' };
export const analysisLabels: Record<string,string> = { pending:'等待分析',running:'正在分析',complete:'分析完成',partial:'已分析一部分',failed:'分析失败',expired:'未分析，原图已过期' };
export const evidenceLabels: Record<string,string> = { available:'原图可用',partial:'部分原图可用',expired:'原图已过期',evicted:'空间不足提前回收',missing:'文件缺失',none:'无证据图片',not_captured:'未采到',outside_schedule:'计划外' };
export const modeLabels: Record<string,string> = { sparse:'稀疏采样',dense:'逐秒分析',mixed:'混合分析' };
export const jobLabels: Record<string,string> = { queued:'排队中',running:'执行中',succeeded:'已完成',partial:'部分完成',failed:'失败',expired:'已过期',cancelled:'已取消' };
export const phaseLabels: Record<string,string> = { waiting:'等待执行',preflight:'检查资料',inference:'分析图片',summarizing:'生成汇总',complete:'处理完成',capturing:'检查视频来源',applying:'应用配置' };
export const queueLabels:Record<string,string> = {scheduled:'等待计划时间',retry_wait:'等待重试',dependency:'等待前置记录',capacity:'等待空闲容量',provider_paused:'模型服务暂停'};
export function captureLabel(runtime:CameraRuntime) {
  if ((runtime.control_revision ?? 0) > (runtime.applied_revision ?? 0)) return '命令已提交，等待应用';
  if (runtime.capture_state==='stopped') return ({disabled:'摄像头已停用',outside_schedule:'当前不在运行时段',manual_override:'已临时停止',resource_pressure:'空间不足，已暂停新图',deleted:'已删除机位'} as Record<string,string>)[runtime.stop_reason??'']??'已停止';
  return ({online:'正在采集',starting:'正在连接',offline:'视频流断开，自动重连中',resource_paused:'空间不足，已暂停新图',error:'采集异常'} as Record<string,string>)[runtime.capture_state??'']??'等待状态';
}
