window.SB_VIEWS = window.SB_VIEWS || {};
window.SB_VIEWS["evaluate-view"] = {
  template: `
  <div class="panel">
    <h2>在线求值</h2>
    <p class="lead">求值只认正式已上屏发布；仅有开放影子时正式求值结果为空播并记拒认痕。</p>
    <div class="row-form">
      <input v-model.number="screen_id" type="number" placeholder="屏幕ID" />
      <input v-model="now_hm" placeholder="HH:MM" />
      <input v-model="weekday" placeholder="星期1-7" />
      <button class="primary" @click="run">求值</button>
    </div>
    <div v-if="result" class="panel" style="margin-top:10px;">
      <p v-if="result.recognized && result.play">
        <span class="badge">已认账</span>
        正式{{ result.source === 'override' ? '单屏覆盖' : '发布' }}
        <template v-if="result.version">v{{ result.version }}</template>
      </p>
      <p v-else-if="result.recognized">
        <span class="badge warn">时段外空播</span> {{ result.reason }}
      </p>
      <p v-else>
        <span class="badge bad">空播 · 未认账</span> {{ result.reason }}
      </p>
      <p v-if="result.rejection" class="err">
        拒认痕：{{ result.rejection.reason }}（影子
        <template v-for="sid in result.rejection.shadow_ids" :key="sid">#{{ sid }} </template>）
      </p>
      <p v-if="result.ledger_id">认账凭证：结果册 #{{ result.ledger_id }}</p>
      <table v-if="playlistItems.length">
        <thead><tr><th>序</th><th>素材</th><th>标题</th><th>驻留秒</th></tr></thead>
        <tbody>
          <tr v-for="it in playlistItems" :key="it.id">
            <td>{{ it.seq }}</td><td>{{ it.asset_code }}</td><td>{{ it.asset_title }}</td><td>{{ it.dwell_sec }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <pre style="white-space:pre-wrap;background:#0d1828;padding:12px;border-radius:10px;">{{ resultText }}</pre>
    <p class="err" v-if="error">{{ error }}</p>
  </div>`,
  data() { return { screen_id: 1, now_hm: "10:30", weekday: "1", result: null, resultText: "", error: "" }; },
  computed: {
    playlistItems() {
      const p = this.result && this.result.play;
      return p && p.kind === "playlist" ? (p.items || []) : [];
    }
  },
  methods: {
    async run() {
      this.error = "";
      try {
        const r = await SB.api("/api/evaluate/screen", {
          method: "POST",
          body: JSON.stringify({ screen_id: this.screen_id, now_hm: this.now_hm, weekday: this.weekday })
        });
        this.result = r;
        this.resultText = JSON.stringify(r, null, 2);
      } catch (e) { this.result = null; this.resultText = ""; this.error = e.message; }
    }
  }
};
