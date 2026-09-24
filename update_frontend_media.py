import os

modal_path = r"D:\AntiGravity\pinguru-landing\src\components\rules\RuleBuilderModal.tsx"
css_path = r"D:\AntiGravity\pinguru-landing\src\styles\rules.css"

with open(modal_path, "r", encoding="utf-8") as f:
    modal_content = f.read()

# 1. Update media grid rendering
old_grid_block = """                      {mediaLoading?(<div className="wizard-media-loading"><RefreshCw size={14} className="animate-spin"/> Loading...</div>):(
                        <div className="wizard-media-grid">
                          {(mediaItems.length>0?mediaItems:COMMENT_MEDIA_PREVIEW).filter(item=>commentFilter==='all'||item.media_type===commentFilter).map(item=>(
                            <button key={item.id} type="button" onClick={()=>setSelectedMediaId(item.id)} className={`wizard-media-card ${selectedMediaId===item.id?'active':''}`}>
                              <span className={`wizard-media-thumb ${item.media_type}`}>{item.media_type==='post'?'▣':'▶'}</span>
                              <span className="wizard-media-label">{item.media_type}</span>
                            </button>
                          ))}
                        </div>
                      )}"""

new_grid_block = """                      {mediaLoading?(<div className="wizard-media-loading"><RefreshCw size={14} className="animate-spin"/> Loading...</div>):(
                        <div className="wizard-media-grid">
                          {(mediaItems.length>0?mediaItems:COMMENT_MEDIA_PREVIEW).filter(item=>commentFilter==='all'||item.media_type===commentFilter).map(item=>{
                            const thumbUrl = item.thumbnail_url || item.media_url;
                            const isReel = item.media_type === 'reel';
                            return (
                              <button
                                key={item.id}
                                type="button"
                                onClick={()=>setSelectedMediaId(item.id)}
                                className={`wizard-media-card ${selectedMediaId===item.id?'active':''}`}
                                title={item.caption || item.media_type}
                              >
                                <div className="wizard-media-thumb-container">
                                  {thumbUrl ? (
                                    <img
                                      src={thumbUrl}
                                      alt={item.caption || item.media_type}
                                      className="wizard-media-img"
                                      referrerPolicy="no-referrer"
                                      loading="lazy"
                                      onError={(e)=>{
                                        e.currentTarget.style.display = 'none';
                                        const fb = e.currentTarget.parentElement?.querySelector('.wizard-media-fallback');
                                        if (fb) (fb as HTMLElement).style.display = 'flex';
                                      }}
                                    />
                                  ) : null}
                                  <span
                                    className={`wizard-media-thumb ${item.media_type} wizard-media-fallback`}
                                    style={{ display: thumbUrl ? 'none' : 'flex' }}
                                  >
                                    {isReel ? '▶' : '▣'}
                                  </span>
                                  <span className="wizard-media-badge">
                                    {isReel ? 'Reel' : 'Post'}
                                  </span>
                                </div>
                                <span className="wizard-media-label" title={item.caption || item.media_type}>
                                  {item.caption ? (item.caption.trim().length > 18 ? item.caption.trim().slice(0, 16) + '...' : item.caption.trim()) : item.media_type}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      )}"""

if old_grid_block in modal_content:
    modal_content = modal_content.replace(old_grid_block, new_grid_block)
    print("Replaced grid block successfully")
else:
    print("WARNING: old_grid_block not found in RuleBuilderModal.tsx")

# 2. Update handleSubmit to populate permalink, caption, type
old_submit_comment = """      ...(triggerType==='comment'&&{
        comment_target_type:commentTarget,
        comment_media_id:commentTarget==='specific'?selectedMediaId:undefined,
        any_comment_keyword:anyCommentKeyword,"""

new_submit_comment = """      ...(triggerType==='comment'&&{
        comment_target_type:commentTarget,
        comment_media_id:commentTarget==='specific'?selectedMediaId:undefined,
        comment_media_permalink:commentTarget==='specific'?mediaItems.find(m=>m.id===selectedMediaId)?.permalink:undefined,
        comment_media_caption:commentTarget==='specific'?mediaItems.find(m=>m.id===selectedMediaId)?.caption:undefined,
        comment_media_type:commentTarget==='specific'?mediaItems.find(m=>m.id===selectedMediaId)?.media_type:undefined,
        any_comment_keyword:anyCommentKeyword,"""

if old_submit_comment in modal_content:
    modal_content = modal_content.replace(old_submit_comment, new_submit_comment)
    print("Replaced submit comment block successfully")
else:
    print("WARNING: old_submit_comment not found")

with open(modal_path, "w", encoding="utf-8") as f:
    f.write(modal_content)

# 3. Update rules.css
with open(css_path, "r", encoding="utf-8") as f:
    css_content = f.read()

old_css = """.wizard-media-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border: 1px solid #d7deea;
  border-radius: 14px;
  background: white;
  transition: transform var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.wizard-media-card.active {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 1px var(--color-primary-ring);
  background: var(--color-primary-light);
}

.wizard-media-thumb {
  width: 82px;
  aspect-ratio: 3 / 4;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.25rem;
  font-weight: 700;
  color: white;
}

.wizard-media-thumb.post {
  background: linear-gradient(180deg, #0f172a 0%, #1d4ed8 100%);
}

.wizard-media-thumb.reel {
  background: linear-gradient(180deg, #111827 0%, #ef4444 100%);
}

.wizard-media-label {
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--color-text-secondary);
}"""

new_css = """.wizard-media-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border: 1px solid #d7deea;
  border-radius: 14px;
  background: white;
  cursor: pointer;
  transition: transform var(--transition-fast), border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.wizard-media-card:hover {
  transform: translateY(-2px);
  border-color: #cbd5e1;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
}

.wizard-media-card.active {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-ring);
  background: var(--color-primary-light);
}

.wizard-media-thumb-container {
  width: 100%;
  aspect-ratio: 3 / 4;
  border-radius: 10px;
  position: relative;
  overflow: hidden;
  background: #f1f5f9;
  display: flex;
  align-items: center;
  justify-content: center;
}

.wizard-media-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.wizard-media-thumb {
  width: 100%;
  height: 100%;
  aspect-ratio: 3 / 4;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.25rem;
  font-weight: 700;
  color: white;
}

.wizard-media-thumb.post {
  background: linear-gradient(180deg, #0f172a 0%, #1d4ed8 100%);
}

.wizard-media-thumb.reel {
  background: linear-gradient(180deg, #111827 0%, #ef4444 100%);
}

.wizard-media-badge {
  position: absolute;
  top: 5px;
  right: 5px;
  background: rgba(15, 23, 42, 0.75);
  backdrop-filter: blur(4px);
  color: white;
  font-size: 0.62rem;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  pointer-events: none;
}

.wizard-media-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--color-text-secondary);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: center;
  line-height: 1.2;
}"""

if old_css in css_content:
    css_content = css_content.replace(old_css, new_css)
    print("Replaced CSS successfully")
else:
    print("WARNING: old_css not found")

with open(css_path, "w", encoding="utf-8") as f:
    f.write(css_content)

print("Done updating frontend files.")
