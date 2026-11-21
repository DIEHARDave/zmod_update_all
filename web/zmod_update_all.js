// zmod_update_all: puts an "Update all components" button back into Zmod's
// Mainsail (Update Manager panel) and Fluidd (Settings > Versions). Both
// forks dropped theirs. The button calls machine.update.full, which the
// plugin's Moonraker component routes to the update queue (update_all.py).
//
// update_all.py adds this file to index.html; the page itself is untouched
// otherwise. This file may be distributed under the terms of the GNU GPLv3.
(function () {
  'use strict'
  if (window.__zmodUpdateAll) return
  window.__zmodUpdateAll = true

  var ID = 'zmod-update-all'
  var SKIP = ['system', 'klipper', 'moonraker']
  var ICON = 'M21,10.12H14.22L16.96,7.3C14.23,4.6 9.81,4.5 7.08,7.2C4.35,9.91 4.35,14.28 7.08,17C9.81,19.7 14.23,19.7 16.96,17C18.32,15.65 19,14.08 19,12.1H21C21,14.08 20.12,16.65 18.36,18.39C14.85,21.87 9.15,21.87 5.64,18.39C2.14,14.92 2.11,9.28 5.62,5.81C9.13,2.34 14.76,2.34 18.27,5.81L21,3V10.12M12.5,8V12.25L16,14.33L15.28,15.54L11,13V8H12.5Z'
  var count = 0
  var busy = false

  function api(path, opts) {
    return fetch(location.origin + path, opts).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j.error && j.error.message) || r.status)
        return j.result
      })
    })
  }

  // update_all.py's _ver_key: [numbers, release?, pre-release parts], or null.
  function verKey(text) {
    var m = /^v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$/.exec(String(text || '').trim())
    if (!m) return null
    var nums = m[1].split('.').map(Number)
    while (nums.length < 4) nums.push(0)
    return [nums, m[2] ? 0 : 1, m[2] ? m[2].split('.') : []]
  }

  function cmpPre(a, b) {
    for (var k = 0; k < Math.min(a.length, b.length); k++) {
      var an = /^\d+$/.test(a[k]), bn = /^\d+$/.test(b[k])
      if (an && bn && +a[k] !== +b[k]) return +a[k] - +b[k]
      if (an !== bn) return an ? -1 : 1
      if (!an && a[k] !== b[k]) return a[k] < b[k] ? -1 : 1
    }
    return a.length - b.length
  }

  function cmpVer(a, b) {
    for (var k = 0; k < Math.max(a[0].length, b[0].length); k++) {
      var d = (a[0][k] || 0) - (b[0][k] || 0)
      if (d) return d
    }
    return a[1] !== b[1] ? a[1] - b[1] : cmpPre(a[2], b[2])
  }

  // A web item offering an older version (Moonraker picks by date).
  function downgrade(i) {
    if (i.configured_type !== 'web') return false
    var have = verKey(i.version), offer = verKey(i.remote_version)
    return !!(have && offer && cmpVer(offer, have) < 0)
  }

  // Same rule as update_all.py: what the queue would install.
  function countUpdates(info) {
    var n = 0
    Object.keys(info).forEach(function (name) {
      var i = info[name]
      if (SKIP.indexOf(name.toLowerCase()) >= 0 || !i.configured_type) return
      if (i.is_valid === false || i.is_dirty || i.corrupt || i.detached ||
          i.channel_invalid || downgrade(i)) return
      var behind = i.configured_type === 'git_repo' && i.commits_behind_count > 0
      var remote = i.remote_version
      if (behind || (remote && remote !== '?' && i.version !== remote)) n++
    })
    return n
  }

  function refreshCount() {
    return api('/machine/update/status').then(function (res) {
      busy = !!res.busy
      count = countUpdates(res.version_info || {})
      render()
    }).catch(function () {})
  }

  function svg() {
    return '<svg viewBox="0 0 24 24" width="18" height="18" ' +
      'style="margin-right:8px;fill:currentColor"><path d="' + ICON +
      '"/></svg>'
  }

  function onClick(ev) {
    ev.preventDefault()
    ev.stopPropagation()
    if (busy || !count) return
    if (!window.confirm('Install all ' + count + ' update(s)? Plugins go ' +
        'first; Z-Mod, Klipper or Moonraker reboot the printer and the ' +
        'queue carries on after each reboot.')) return
    busy = true
    render()
    api('/machine/update/full', { method: 'POST' }).catch(function (e) {
      window.alert('Update all failed: ' + e.message)
    }).then(function () {
      setTimeout(refreshCount, 5000)
    })
  }

  function makeButton(cls) {
    var b = document.createElement('button')
    b.type = 'button'
    b.className = 'v-btn theme--dark primary--text ' + cls
    b.innerHTML = '<span class="v-btn__content">' + svg() +
      '<span class="zua-label"></span></span>'
    b.addEventListener('click', onClick)
    return b
  }

  function label(el) {
    var l = el.querySelector('.zua-label')
    var text = 'Update all components' + (count > 1 ? ' (' + count + ')' : '')
    // Only on change: our own DOM writes would retrigger the observer.
    if (l && l.textContent !== text) l.textContent = text
    var b = el.tagName === 'BUTTON' ? el : el.querySelector('button')
    if (b) {
      b.disabled = busy
      b.classList.toggle('v-btn--disabled', busy)
    }
  }

  // Mainsail: a row under the list in the Update Manager panel.
  function mainsail() {
    var list = document.querySelector('.machine-update-panel .update-manager-list')
    if (!list) return null
    var el = document.getElementById(ID)
    if (!el || el.parentNode !== list.parentNode) {
      if (el) el.remove()
      el = document.createElement('div')
      el.id = ID
      el.innerHTML = '<hr class="v-divider theme--dark" style="margin:0">' +
        '<div style="display:flex;justify-content:center;padding:8px 0"></div>'
      el.lastChild.appendChild(makeButton('v-btn--text v-size--default'))
      list.parentNode.insertBefore(el, list.nextSibling)
    }
    return el
  }

  // Fluidd: next to "Check for updates" in Settings > Versions.
  function fluidd() {
    var head = document.getElementById('versions')
    var card = head && head.nextElementSibling
    var check = card && card.querySelector('.v-btn')
    if (!check) return null
    var el = document.getElementById(ID)
    if (!el || el.previousElementSibling !== check) {
      if (el) el.remove()
      el = makeButton('v-btn--outlined v-size--small')
      el.id = ID
      el.style.marginLeft = '8px'
      check.parentNode.insertBefore(el, check.nextSibling)
    }
    return el
  }

  function render() {
    var el = mainsail() || fluidd()
    if (!el) return
    el.style.display = count > 0 ? '' : 'none'
    label(el)
  }

  var queued = false
  new MutationObserver(function () {
    if (queued) return
    queued = true
    requestAnimationFrame(function () {
      queued = false
      render()
    })
  }).observe(document.documentElement, { childList: true, subtree: true })

  refreshCount()
  setInterval(refreshCount, 30000)
})()
