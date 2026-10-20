(function(){
  var dot=document.getElementById('ai-dot');
  var dotStatus=document.getElementById('ai-dot-status');
  if(dot){
    fetch('/api/ai/status',{method:'POST'})
      .then(r=>r.json())
      .then(d=>{
        var label = d.status==='ok' ? ('AI ready — '+d.model) : 'AI unavailable';
        if(d.status==='ok'){dot.style.background='#4f4';}
        else{dot.style.background='#888';}
        dot.title=label;
        if(dotStatus) dotStatus.textContent=label;
      }).catch(()=>{});
  }
  var THEMES=['dark','dim','light','contrast'];
  var THEME_LABELS={dark:'🌑 Dark',dim:'🌗 Dim',light:'☀ Light',contrast:'◐ Contrast'};
  var themeBtn=document.getElementById('theme-toggle');
  if(themeBtn){
    var setTheme=function(t){
      if(window.ndSite){ window.ndSite.set({theme:t}); }       // this browser, and the account when signed in
      else{ document.documentElement.dataset.theme=t; try{localStorage.setItem('nd_theme',t);}catch(e){} }
      themeBtn.textContent=THEME_LABELS[t]||THEME_LABELS.dark;
    };
    setTheme(document.documentElement.dataset.theme||'dark');
    themeBtn.addEventListener('click',function(){
      var idx=THEMES.indexOf(document.documentElement.dataset.theme);
      setTheme(THEMES[(idx+1)%THEMES.length]);
    });
  }
  // UI scale — same per-browser localStorage pattern as theme above. Exposed
  // on window so a second control elsewhere on the page (Settings > Options)
  // can drive the exact same state instead of duplicating this logic.
  window.ndSetUIScale=function(v){
    if(window.ndSite){ window.ndSite.set({scale:String(v)}); }
    else{ document.documentElement.dataset.scale=v; try{localStorage.setItem('nd_ui_scale',v);}catch(e){} }
    document.querySelectorAll('.nd-ui-scale-select').forEach(function(el){el.value=v;});
  };
  var scaleSelect=document.getElementById('scale-select');
  if(scaleSelect){
    scaleSelect.classList.add('nd-ui-scale-select');
    scaleSelect.value=document.documentElement.dataset.scale||'100';
    scaleSelect.addEventListener('change',function(){window.ndSetUIScale(this.value);});
  }
  // Lets a GM drag a nav tab (Character, Race Catalog, Boards, ...) onto
  // the home page's Quick Links — see index.html for the drop targets.
  // Kind links carry data-ql-type="kind" (they map onto the existing
  // target_type="kind" link schema); every other draggable link carries
  // its own data-ql-ref too (the bare, undecorated path) so the drag
  // payload always uses a clean target_type="url" ref even though the
  // visible href now carries a ?w=<world slug> query param — otherwise a
  // dropped Quick Link would freeze onto whatever world happened to be
  // active at drag time instead of following the viewer's current world
  // like every other nav link.
  document.addEventListener('dragstart', function(e){
    var a = e.target.closest('a[draggable="true"]');
    if (!a) return;
    e.dataTransfer.setData('application/json', JSON.stringify({
      label: a.textContent.trim(),
      target_type: a.dataset.qlType || 'url',
      target_ref: a.dataset.qlRef || a.getAttribute('href'),
    }));
    e.dataTransfer.effectAllowed = 'copy';
  });
  var toggle=document.getElementById('nav-toggle');
  var nav=document.getElementById('nav-kinds');
  if(toggle&&nav){
    toggle.addEventListener('click',function(e){
      e.stopPropagation();
      nav.classList.toggle('open');
      toggle.textContent=nav.classList.contains('open')?'✕':'☰';
    });
    document.addEventListener('click',function(e){
      if(!e.target.closest('.nav-kinds')&&!e.target.closest('#nav-toggle')){
        nav.classList.remove('open');
        toggle.textContent='☰';
      }
    });
  }
})();
