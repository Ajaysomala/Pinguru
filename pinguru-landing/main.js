const FORMSPREE_URL = 'https://formspree.io/f/mykbvqoj';

// FAQ Toggle
function toggleFaq(btn) {
  const item = btn.closest('.faq-item');
  const isOpen = item.classList.contains('open');
  document.querySelectorAll('.faq-item.open').forEach(el => el.classList.remove('open'));
  if (!isOpen) item.classList.add('open');
}

// Email Waitlist Submit
async function handleSubmit(e) {
  e.preventDefault();
  const emailInputs = [
    document.getElementById('email-input'),
    document.getElementById('email-input-2')
  ];
  const email = emailInputs.find(el => el && el.value)?.value;

  try {
    const resp = await fetch(FORMSPREE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, source: 'pinguru-landing' })
    });

    if (resp.ok) {
      document.getElementById('success-msg').style.display = 'block';
      emailInputs.forEach(el => { if (el) el.value = ''; });

      const count = document.querySelector('.proof-text strong');
      if (count) {
        const n = parseInt(count.textContent) + 1;
        count.textContent = n + ' creators';
      }
    }
  } catch {
    document.getElementById('success-msg').style.display = 'block';
    emailInputs.forEach(el => { if (el) el.value = ''; });
  }
}

// Entrance Animations
const observer = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.style.opacity = '1';
      entry.target.style.transform = 'translateY(0)';
    }
  });
}, { threshold: 0.1 });

document.querySelectorAll('.step-card, .feature-card, .price-card, .testimonial').forEach(el => {
  el.style.opacity = '0';
  el.style.transform = 'translateY(24px)';
  el.style.transition = 'opacity 0.5s ease, transform 0.5s ease';
  observer.observe(el);
});