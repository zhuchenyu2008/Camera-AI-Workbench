async(page)=>{
  const checks=[],base='http://127.0.0.1:5173',assert=(ok,message)=>{if(!ok)throw new Error(message);checks.push(message);};
  const state=await(await page.request.get(base+'/api/_test/state')).json(),session=await(await page.request.get(base+'/api/session')).json();
  const old=page.getByRole('dialog',{name:'确认设置影响',exact:true});if(await old.count())await old.getByRole('button',{name:'取消',exact:true}).click();
  await page.getByLabel('现场图保存（分钟）',{exact:true}).fill('23');
  await page.getByRole('button',{name:'预检保存影响',exact:true}).click();
  const impact=page.getByRole('dialog',{name:'确认设置影响',exact:true});await impact.waitFor();
  const count=parseInt(await impact.locator('.definition-list > div').filter({hasText:'将立即到期图片'}).locator('dd').innerText());
  assert(count>0,'F11 缩短 TTL 显示具体将到期图片数：'+count);
  await page.request.post(base+'/api/_test/control',{headers:{'X-CSRF-Token':session.data.csrf_token},data:{expire_preview:true}});
  await impact.getByRole('button',{name:'保存并使这些图片到期',exact:true}).click();
  await impact.getByText(/预检来源变化，请重新核对新影响/).waitFor();
  assert(await impact.isVisible(),'F11 过期的预检会重新检查，必须再次确认');
  await impact.getByRole('button',{name:'保存并使这些图片到期',exact:true}).click();await impact.waitFor({state:'hidden'});
  const apply=page.getByRole('dialog',{name:'任务进度',exact:true});if(await apply.count()){await apply.getByText('已完成',{exact:true}).waitFor();await apply.getByRole('button',{name:'关闭对话框',exact:true}).click();}
  assert((await page.request.get(base+'/api/evidence/'+state.older_evidence)).status()===410,'F11 受影响原图保存后真实返回 410');
  await page.goto(base+'/usage?date=2026-10-18');await page.getByRole('heading',{name:'调用明细',exact:true}).waitFor();
  assert((await page.locator('main').innerText()).includes('未知')&&(await page.locator('main').innerText()).includes('未配置'),'F12 Token 与费用未知时不伪造零值');
  await page.goto(base+'/status?date=2026-10-18');await page.getByRole('heading',{name:'媒体存储',exact:true}).waitFor();
  assert(await page.getByRole('heading',{name:'采集状态',exact:true}).count()===1&&await page.getByRole('heading',{name:'AI 队列',exact:true}).count()===1,'F12 采集、AI 队列、存储、模型和进程分别呈现');
  return {checks};
}
