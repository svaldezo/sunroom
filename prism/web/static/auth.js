/* Sunroom — sign-in.

   Magic links, through Supabase Auth. There is no password anywhere in this
   file and no password anywhere in the product: the whole flow is "type your
   email, click the link in it", which is fewer things for someone to lose and
   fewer things for us to store.

   Two details are worth knowing.

   The access token is short-lived and supabase-js refreshes it in the
   background. `auth.token()` therefore reads the current value on every call
   rather than caching one at start-up -- caching it is how an app works
   perfectly for an hour and then 401s until you reload.

   And when there is no Supabase configured at all, this deployment is a
   single-user one: `auth.ready()` resolves immediately and the app runs with no
   sign-in screen. That is the local case, and the server refuses to serve it in
   production. */

const auth = (function () {
  let client = null;
  let session = null;
  let config = null;
  let onChange = () => {};

  async function load() {
    config = await fetch('/api/config').then(r => r.json()).catch(() => ({}));
    if (!config.multi_user) return {mode: 'local', config};

    // supabase-js is loaded from the CDN in index.html; if it did not arrive,
    // say so plainly rather than presenting a sign-in form that cannot work.
    if (!window.supabase || !window.supabase.createClient) {
      return {mode: 'broken', config,
              error: 'Sunroom could not load its sign-in library. Check your '
                     + 'connection and reload.'};
    }
    client = window.supabase.createClient(
      config.supabase_url, config.supabase_anon_key,
      {auth: {persistSession: true, autoRefreshToken: true,
              detectSessionInUrl: true, flowType: 'pkce'}});

    const {data} = await client.auth.getSession();
    session = data.session || null;
    client.auth.onAuthStateChange((_event, next) => {
      const was = !!session;
      session = next || null;
      if (was !== !!session) onChange(!!session);
    });
    return {mode: 'supabase', config};
  }

  return {
    async ready(cb) {
      onChange = cb || onChange;
      return load();
    },
    get config() { return config || {}; },
    get multiUser() { return !!(config && config.multi_user); },
    signedIn() { return !this.multiUser || !!session; },
    // Read live: supabase-js rotates this behind our back.
    token() { return session && session.access_token; },
    email() { return (session && session.user && session.user.email) || ''; },

    async sendLink(email) {
      if (!client) throw new Error('Sign-in is not configured.');
      const {error} = await client.auth.signInWithOtp({
        email: String(email || '').trim(),
        options: {emailRedirectTo: window.location.origin},
      });
      if (error) throw new Error(error.message);
    },

    async signOut() {
      if (client) await client.auth.signOut();
      session = null;
      onChange(false);
    },

    /* Called when the API answers 401. The token is gone or was never good, so
       drop the session and let the app show the sign-in screen. */
    expired() {
      if (!this.multiUser) return;
      session = null;
      onChange(false);
    },
  };
})();

window.auth = auth;
