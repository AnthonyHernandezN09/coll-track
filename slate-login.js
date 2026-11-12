function submitSlateLogin(expectedUrl, email, password) {
  const url = new URL(location.href);
  if (url.href !== expectedUrl || url.protocol !== 'https:' || url.port ||
      url.pathname !== '/account/login') return 'changed';
  const returns = url.searchParams.getAll('r');
  try {
    if (returns.some(value => new URL(value, url).origin !== url.origin)) return 'unsupported';
  } catch {
    return 'unsupported';
  }

  if (email !== undefined && document.querySelector('iframe[src*="captcha" i], [data-sitekey]')) {
    return 'verification';
  }

  const forms = [...document.querySelectorAll('form[data-fw-form="1"]')];
  const matching = forms.flatMap(form => {
    const user = form.querySelector('input[type="email"][name="email"]');
    const secret = form.querySelector('input[type="password"][name="password"]');
    const submit = form.querySelector('button[type="submit"]');
    if (!user || !secret || !submit || !/FW\.Postback\(this\)/.test(submit.getAttribute('onclick') ?? '')) {
      return [];
    }
    return [{ form, user, secret, submit }];
  });
  if (matching.length !== 1) return 'unsupported';
  const { form, user, secret, submit } = matching[0];
  if (form.method !== 'post' || new URL(form.action, url).origin !== url.origin ||
      submit.hasAttribute('formaction') || submit.hasAttribute('formtarget') ||
      submit.hasAttribute('formmethod') || form.target ||
      [user, secret, submit].some(field => field.matches(':disabled') || !field.getClientRects().length) ||
      user.readOnly || secret.readOnly) return 'unsupported';

  if (email === undefined) return 'supported';

  user.value = email;
  secret.value = password;
  submit.click();
  return 'submitted';
}
