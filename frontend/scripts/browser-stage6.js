async(page)=>{
  const checks=[],base='http://127.0.0.1:5173',assert=(ok,message)=>{if(!ok)throw new Error(message);checks.push(message);};
  const otherContext=await page.context().browser().newContext();
  try{
    const other=await otherContext.newPage();await other.goto(base+'/login');await other.getByLabel('用户名',{exact:true}).fill('frontend-test');await other.getByLabel('密码',{exact:true}).fill('frontend-browser-test-2026');await other.getByRole('button',{name:'登录',exact:true}).click();await other.getByRole('heading',{name:'今日概览',exact:true}).waitFor();
    await page.goto(base+'/account');await page.getByLabel('当前密码',{exact:true}).fill('frontend-browser-test-2026');await page.getByLabel('新密码',{exact:true}).fill('frontend-browser-test-updated-2026');await page.getByLabel('再次输入新密码',{exact:true}).fill('frontend-browser-test-updated-2026');await page.getByRole('button',{name:'修改密码并退出所有会话',exact:true}).click();await page.getByRole('heading',{name:'登录工作台',exact:true}).waitFor();
    assert((await other.request.get(base+'/api/session')).status()===401,'F01 修改密码后另一独立浏览器会话失效');
    await page.getByLabel('用户名',{exact:true}).fill('frontend-test');await page.getByLabel('密码',{exact:true}).fill('frontend-browser-test-updated-2026');await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByRole('heading',{name:'今日概览',exact:true}).waitFor();
    assert(new URL(page.url()).pathname==='/','F01 新密码可重新登录，跳转仅限应用内');
    await page.goto(base+'/account');await page.getByRole('button',{name:'退出登录',exact:true}).click();await page.getByRole('heading',{name:'登录工作台',exact:true}).waitFor();
    assert((await page.request.get(base+'/api/session')).status()===401,'F01 退出删除会话，受保护 API 返回 401');
    assert(await page.locator('video').count()===0&&await page.evaluate(()=>localStorage.length===0),'F01 退出后媒体与敏感缓存清理');
  }finally{await otherContext.close();}
  return {checks};
}
