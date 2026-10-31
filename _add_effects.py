p='e:/proj/ai-hot-monitor/web/styles.css'
s=open(p,encoding='utf-8').read()
add='''

/* ===== settings contrast fix ===== */
.settings-section { background: rgba(17,24,39,.62); backdrop-filter: blur(10px); border: 1px solid var(--line); }
.settings-title h2 { color: #f1f5f9; }
.settings-title p { color: #cbd5e1; }
.setting-block label { color: #cbd5e1; }
.setting-heading h3 { color: #f1f5f9; }
.setting-heading p { color: #94a3b8; }
.setting-block input::placeholder { color: #64748b; }
.setting-block select { color: #e2e8f0; background: rgba(15,23,42,.7); }
.local-badge { color: #cbd5e1; background: rgba(30,41,59,.7); }
.connection-status, .notification-actions { color: #cbd5e1; }
.section-kicker { color: #94a3b8; }

/* ===== extra tech effects: aurora + meteors (subtle) ===== */
.aurora {
  position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
}
.aurora::before, .aurora::after {
  content: ''; position: absolute; width: 60vw; height: 60vw; border-radius: 50%;
  filter: blur(80px); opacity: .25; animation: aurora-drift 22s ease-in-out infinite alternate;
}
.aurora::before { background: #3b82f6; top: -20%; left: -10%; }
.aurora::after { background: #22d3ee; bottom: -25%; right: -15%; animation-delay: -11s; }
@keyframes aurora-drift { from { transform: translate(0,0) scale(1); } to { transform: translate(8%,6%) scale(1.1); } }

.meteor {
  position: fixed; top: -10%; left: 0; width: 2px; height: 80px;
  background: linear-gradient(180deg, rgba(34,211,238,.9), transparent);
  opacity: .6; pointer-events: none; z-index: 0;
  animation: meteor-fall 6s linear infinite;
}
@keyframes meteor-fall {
  0% { transform: translate(0,0) rotate(25deg); opacity: 0; }
  10% { opacity: .6; }
  100% { transform: translate(40vw, 110vh) rotate(25deg); opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .aurora::before, .aurora::after, .meteor { animation: none !important; }
}
'''
s=s.rstrip()+'\n'+add
open(p,'w',encoding='utf-8').write(s)
print('done')
'''