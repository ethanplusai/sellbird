// Dashboard display language. Keep technical values, user content and API data
// unchanged; translate only exact, known interface strings.
const DASHBOARD_LANG_KEY = 'sellbird-dashboard-language';
const PT_BR = {
  'Make the first move.': 'Dê o primeiro passo.',
  'Work': 'Trabalho', 'Today': 'Hoje', 'Your company': 'Sua empresa',
  'Outbox': 'Caixa de saída', 'Inboxes': 'Caixas de entrada',
  'Conversations': 'Conversas', 'Pipeline': 'Funil', 'Signals': 'Sinais',
  'Discover': 'Descobrir', 'Companies': 'Empresas', 'Contacts': 'Contatos',
  'Campaigns': 'Campanhas', 'System': 'Sistema', 'Activity': 'Atividade',
  'Usage': 'Uso', 'Controls': 'Controles', 'Settings': 'Configurações',
  'Help': 'Ajuda', 'Follow @sellbirdagent': 'Siga @sellbirdagent',
  'News and updates on X': 'Notícias e atualizações no X',
  'Appearance': 'Aparência', 'Match your computer': 'Seguir o computador',
  'Light': 'Claro', 'Dark': 'Escuro', 'Refresh this page': 'Atualizar esta página',
  'Refresh': 'Atualizar', 'Checking…': 'Verificando…',
  'Dashboard locked': 'Painel bloqueado', 'Dashboard token': 'Token do painel',
  'Unlock': 'Desbloquear',
  "What's waiting on you, and how the pipeline is moving.": 'O que aguarda sua decisão e como o funil está avançando.',
  'Needs you': 'Precisa de você', 'Pipeline right now': 'Funil agora',
  'Contacts by stage': 'Contatos por etapa', 'Recent activity': 'Atividade recente',
  'See all': 'Ver todos', 'Loading...': 'Carregando...',
  'Yesterday': 'Ontem', 'No results': 'Nenhum resultado',
  'Emails waiting for you. Read one, decide, move on —': 'E-mails aguardando sua decisão. Leia, decida e avance —',
  'approve,': 'aprove,', 'edit,': 'edite,', 'reject,': 'recuse,',
  'to move.': 'para avançar.',
  'The email addresses Sellbird sends from. Each one sends a limited number of cold emails a day, so more inboxes means more emails out. Replies stay on the inbox that started the thread.': 'Os endereços usados pelo Sellbird para enviar e-mails. Cada caixa tem um limite diário; as respostas permanecem na caixa que iniciou a conversa.',
  'Every reply, and how Sellbird handled it.': 'Cada resposta e como o Sellbird lidou com ela.',
  'Sellbird proposes what it knows how to look for. You decide what actually defines a good prospect — nothing is collected until you confirm it.': 'O Sellbird sugere sinais que consegue identificar. Você decide o que define um bom prospecto; nada é coletado até sua confirmação.',
  'Find businesses matching your ICP. Pick a source, see what it will cost, then run it — nothing is called until you press the button.': 'Encontre empresas que correspondem ao seu cliente ideal. Escolha uma fonte, veja o custo e inicie a busca.',
  'Organizations Sellbird has researched, best fit first. Click a row to see its contacts.': 'Empresas pesquisadas pelo Sellbird, começando pelas mais alinhadas. Clique em uma linha para ver os contatos.',
  'People Sellbird has found, how sure it is of their address, and where each one stands.': 'Pessoas encontradas, confiança no endereço de e-mail e etapa de cada contato.',
  'Email sequences Sellbird has written, and how far each one has got.': 'Sequências de e-mails criadas pelo Sellbird e o andamento de cada uma.',
  'A running log of every action Sellbird\'s agents have taken.': 'Registro das ações realizadas pelos agentes do Sellbird.',
  "Model usage on your subscription (Claude or Codex): calls and tokens by provider, agent and task. Subscription plans aren't billed per token, so there are no dollar costs here.": 'Uso de modelos na sua assinatura (Claude ou Codex): chamadas e tokens por provedor, agente e tarefa. Assinaturas não são cobradas por token, então não há valores em dólar aqui.',
  'Credentials are stored locally in': 'As credenciais são armazenadas localmente em',
  'and never sent anywhere except the services themselves.': 'e enviadas apenas aos respectivos serviços.',
  'Language': 'Idioma', 'Dashboard language': 'Idioma do painel',
  'Choose the language used in this browser. This does not change the language of emails written for prospects.': 'Escolha o idioma exibido neste navegador. Isso não altera o idioma dos e-mails escritos para prospectos.',
  'Sending': 'Envio', 'Email provider': 'Provedor de e-mail',
  'Active:': 'Ativo:', 'Recommended': 'Recomendado', 'Legacy': 'Legado',
  'Strongly recommended': 'Altamente recomendado', 'Optional': 'Opcional',
  'Email verification': 'Verificação de e-mail', 'Verifiers': 'Verificadores',
  'Add at least one so found emails get verified. Without any, addresses stay': 'Adicione pelo menos um para verificar os e-mails encontrados. Sem um verificador, os endereços ficam como',
  'and are never sent. Reoon has the best free tier (600 a month).': 'e nunca são usados para envio. O Reoon oferece 600 verificações grátis por mês.',
  'guess': 'estimativa', 'not set': 'não configurado',
  'Gmail / Google Workspace': 'Gmail / Google Workspace',
  'SMTP + IMAP mailbox': 'Caixa de e-mail SMTP + IMAP',
  'Any real mailbox (AgentMail, Fastmail, a Workspace app password). IMAP defaults to the SMTP host and login if left blank.': 'Use uma caixa de e-mail real (AgentMail, Fastmail ou senha de app do Workspace). Se os campos IMAP ficarem vazios, o servidor e usuário SMTP serão usados.',
  'Use a mailbox on a dedicated secondary domain. Create an OAuth client (type "Desktop app") in Google Cloud Console → Gmail API, paste the id and secret, save, then sign in on the Inboxes tab (or run': 'Use uma caixa em um domínio secundário. Crie um cliente OAuth do tipo “Aplicativo para computador” no Google Cloud Console → Gmail API, informe o ID e o segredo, salve e conecte a conta em Caixas de entrada (ou execute',
  'Client ID': 'ID do cliente', 'Client secret': 'Segredo do cliente',
  'Enter client secret': 'Digite o segredo do cliente',
  'Save Gmail': 'Salvar Gmail', 'SMTP host': 'Servidor SMTP',
  'Port': 'Porta', 'Username': 'Usuário', 'Password': 'Senha',
  'Enter password / app password': 'Digite a senha ou senha de aplicativo',
  'Reading replies from a different IMAP server or login': 'Ler respostas em outro servidor ou usuário IMAP',
  'IMAP host': 'Servidor IMAP', 'IMAP username': 'Usuário IMAP',
  'IMAP password': 'Senha IMAP', 'Same as SMTP host': 'Mesmo servidor SMTP',
  'Same as SMTP username': 'Mesmo usuário SMTP',
  'Same as SMTP password': 'Mesma senha SMTP',
  'Use the SMTP password for IMAP': 'Usar a senha SMTP no IMAP',
  'Save SMTP': 'Salvar SMTP',
  'Microsoft turned off password sign-in for IMAP, so Outlook mailboxes connect with OAuth. In': 'A Microsoft desativou o login por senha no IMAP; caixas Outlook usam OAuth. Em',
  '→ App registrations, register an app for your organization only and add the redirect URI': '→ Registros de aplicativos, registre um aplicativo para sua organização e adicione o URI de redirecionamento',
  'as "Public client/native". Paste both IDs here, save, then sign in on the Inboxes tab (or run': 'como “Cliente público/nativo”. Informe os dois IDs, salve e conecte a conta em Caixas de entrada (ou execute',
  '). No client secret is needed.': '). Não é necessário segredo do cliente.',
  'Application (client) ID': 'ID do aplicativo (cliente)',
  'Directory (tenant) ID': 'ID do diretório (locatário)',
  'Save Microsoft 365': 'Salvar Microsoft 365',
  'Get your API key from': 'Obtenha sua chave de API em',
  '(Growth plan or higher for API).': '(plano Growth ou superior para usar a API).',
  'API key': 'Chave de API', 'API token': 'Token de API',
  'Enter your Instantly API key': 'Digite sua chave de API do Instantly',
  'Show': 'Mostrar', 'Test': 'Testar', 'Save Instantly': 'Salvar Instantly',
  'Reoon API key': 'Chave de API Reoon',
  'ZeroBounce API key': 'Chave de API ZeroBounce',
  'Hunter API key': 'Chave de API Hunter',
  'Save verification keys': 'Salvar chaves de verificação',
  'For automated LinkedIn prospecting. Sellbird logs in and searches like a human.': 'Para prospectar no LinkedIn. O Sellbird entra na conta e faz buscas pelo navegador.',
  'Email or username': 'E-mail ou usuário',
  'Enter password': 'Digite a senha', 'Save LinkedIn': 'Salvar LinkedIn',
  'Deep website crawling with JavaScript rendering during product training. About $5 a month.': 'Análise profunda de sites com renderização JavaScript durante o treinamento do produto. Cerca de US$ 5 por mês.',
  'Account ID': 'ID da conta',
  'Your Cloudflare Account ID': 'ID da sua conta Cloudflare',
  'Your Cloudflare API Token': 'Token de API da Cloudflare',
  'Save Cloudflare': 'Salvar Cloudflare',
  'Start and stop Sellbird\'s heartbeat loop, and watch what it\'s doing.': 'Inicie ou pare o ciclo do Sellbird e acompanhe o que ele está fazendo.',
  'Agent': 'Agente', 'Stopped': 'Parado',
  'Start Sellbird': 'Iniciar Sellbird', 'Stop Sellbird': 'Parar Sellbird',
  'Recent logs': 'Registros recentes',
  'No logs yet. Start Sellbird to see activity.': 'Ainda não há registros. Inicie o Sellbird para acompanhar a atividade.',
  'What is Sellbird?': 'O que é o Sellbird?',
  'Getting started': 'Primeiros passos', 'Common issues': 'Problemas comuns',
  'Getting your API keys': 'Obtendo suas chaves de API',
  'Where everything lives': 'Onde ficam os arquivos',
  'What Sellbird is, how it works, and how to fix the usual problems.': 'O que é o Sellbird, como funciona e como resolver problemas comuns.',
  'Sellbird is an open-source outreach platform. Once set up, Sellbird runs on its own: finds people who match your ideal customer, writes personalized cold emails, sends them through your email platform, reads every reply, handles objections, and works toward booking a meeting. You review everything through this dashboard.': 'O Sellbird é uma plataforma de prospecção de código aberto. Depois de configurado, encontra pessoas que correspondem ao seu cliente ideal, escreve e envia e-mails personalizados, lê as respostas e acompanha as conversas até uma reunião. Você acompanha tudo neste painel.',
  'There are three things to do:': 'Há três etapas:',
  'Today shows exactly what\'s done and what still needs to happen.': 'A tela Hoje mostra o que já foi feito e o que ainda falta.',
  'Everything Sellbird needs is inside this one project folder. Nothing is stored elsewhere.': 'Tudo de que o Sellbird precisa fica na pasta deste projeto.',
  'Your product info, target customers, and behavior settings': 'Produto, clientes ideais e configurações de comportamento',
  'Your API keys and credentials (never shared or committed)': 'Chaves de API e credenciais (não compartilhe nem envie ao Git)',
  'Database with all prospects, campaigns, and conversations': 'Banco de dados com prospectos, campanhas e conversas',
  'Log file showing what Sellbird is doing': 'Registro das ações do Sellbird',
  'Prompt templates for each agent. Advanced customization.': 'Modelos de prompt de cada agente. Personalização avançada.',
  'Sales knowledge files. Edit these to change how Sellbird writes and sells.': 'Arquivos de conhecimento comercial. Edite para ajustar a escrita e a abordagem de vendas.',
  'Instantly is the legacy sending option; Gmail or SMTP is recommended.': 'Instantly é a opção de envio legada; recomendamos Gmail ou SMTP.',
  'LinkedIn credentials (optional)': 'Credenciais do LinkedIn (opcional)',
  'Cloudflare Browser Rendering (optional)': 'Renderização de navegador Cloudflare (opcional)',
  'If you skip this, Sellbird will find prospects through Google searches and company website scraping instead.': 'Se você pular esta etapa, o Sellbird buscará prospectos pelo Google e pelos sites das empresas.',
  'If you want Sellbird to find prospects on LinkedIn, enter your LinkedIn email and password. Sellbird uses a real browser to search LinkedIn like a human would, with random delays and rate limits to avoid detection.': 'Para encontrar prospectos no LinkedIn, informe seu usuário e senha. O Sellbird usa um navegador para fazer as buscas com intervalos e limites de frequência.',
  'This is only used during product training (when Sellbird crawls your website to learn about your product). It handles JavaScript-heavy websites that a basic crawler can\'t read.': 'Usado apenas no treinamento do produto, ao ler seu site. Permite analisar páginas com muito JavaScript.',
  'Without this, Sellbird uses a built-in crawler that works fine for most websites but can\'t render JavaScript.': 'Sem isso, o Sellbird usa o leitor integrado, que atende à maioria dos sites, mas não renderiza JavaScript.',
  'Sellbird isn\'t finding prospects': 'O Sellbird não encontra prospectos',
  'Check that your ICP (ideal customer profile) in sellbird.yaml has realistic titles, industries, and geography. If LinkedIn is set up, check that the credentials are correct. Check the Activity tab to see what Sellbird has been trying to do.': 'Confira se o perfil do cliente ideal em sellbird.yaml tem cargos, setores e regiões adequados. Se usar LinkedIn, confira as credenciais. Veja a aba Atividade para entender as tentativas de busca.',
  'Instantly API returns 401': 'A API do Instantly retorna 401',
  'Your API key is wrong, or you need the Growth plan (the free plan doesn\'t include API access). Double-check the key in Settings > Integrations in your Instantly dashboard.': 'Confira sua chave de API e o plano Growth: o plano gratuito não inclui acesso à API. Verifique em Configurações > Integrações no Instantly.',
  'You need to activate the virtual environment first:': 'Primeiro, ative o ambiente virtual:',
  'Source': 'Fonte', 'Run': 'Executar', 'Estimate first': 'Estimar primeiro',
  'Estimate cost': 'Estimar custo', 'Export all': 'Exportar tudo',
  'Export deliverable': 'Exportar entregáveis', 'Export register': 'Exportar registro',
  'Search depth': 'Profundidade da busca', 'Results per query': 'Resultados por busca',
  'Spend cap ($)': 'Limite de gastos (US$)',
  'Cities': 'Cidades', 'Your ICP\'s industries': 'Setores do seu cliente ideal',
  'Saved — enter new value to change': 'Salvo — digite um novo valor para alterar',
  'Saved. Now sign in on the Inboxes tab.': 'Salvo. Agora conecte sua conta em Caixas de entrada.',
  'SMTP settings saved.': 'Configurações SMTP salvas.',
  'Verification keys saved.': 'Chaves de verificação salvas.',
  'New': 'Novo', 'Contacted': 'Contatado', 'Replied': 'Respondeu',
  'Interested': 'Interessado', 'Meeting booked': 'Reunião marcada',
  'Not interested': 'Sem interesse', 'Bounced': 'E-mail rejeitado',
  'Queued': 'Na fila', 'Draft': 'Rascunho', 'Active': 'Ativo',
  'Completed': 'Concluído', 'Paused': 'Pausado', 'Open': 'Aberto',
  'Closed': 'Fechado', 'Won': 'Ganho', 'Lost': 'Perdido',
  'Question': 'Pergunta', 'Objection': 'Objeção',
  'Out of office': 'Ausente', 'Wrong person': 'Pessoa errada',
  'Unsubscribed': 'Descadastrado', 'Needs a human': 'Precisa de uma pessoa',
  'Thinking...': 'Pensando...', 'Suggest ideas': 'Sugerir ideias',
  'Preview an email': 'Prévia de um e-mail'
};

let dashboardLanguage = 'en';
const originalText = new WeakMap();
const originalAttributes = new WeakMap();
const TRANSLATED_ATTRIBUTES = ['placeholder', 'title', 'aria-label'];

function dashboardLocale() { return dashboardLanguage === 'pt-BR' ? 'pt-BR' : 'en-US'; }

function translateKnown(raw) {
  const match = /^(\s*)([\s\S]*?)(\s*)$/.exec(raw);
  const key = match[2].replace(/\s+/g, ' ').trim();
  return Object.prototype.hasOwnProperty.call(PT_BR, key)
    ? match[1] + PT_BR[key] + match[3] : raw;
}

function applyDashboardLanguage() {
  document.documentElement.lang = dashboardLanguage === 'pt-BR' ? 'pt-BR' : 'en';
  const selector = document.getElementById('dashboard-language');
  if (selector) selector.value = dashboardLanguage;

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (!parent || parent.closest('script, style, code, pre, textarea, #dashboard-language, [data-no-translate]')) continue;
    let source = originalText.get(node);
    if (source === undefined || (node.nodeValue !== source && node.nodeValue !== translateKnown(source))) {
      source = node.nodeValue;
      originalText.set(node, source);
    }
    const desired = dashboardLanguage === 'pt-BR' ? translateKnown(source) : source;
    if (node.nodeValue !== desired) node.nodeValue = desired;
  }
  document.querySelectorAll('[placeholder], [title], [aria-label]').forEach(el => {
    if (el.closest('code, pre, #dashboard-language, [data-no-translate]')) return;
    let originals = originalAttributes.get(el);
    if (!originals) { originals = {}; originalAttributes.set(el, originals); }
    TRANSLATED_ATTRIBUTES.forEach(attr => {
      const value = el.getAttribute(attr);
      if (value === null) return;
      if (originals[attr] === undefined || (value !== originals[attr] && value !== translateKnown(originals[attr]))) originals[attr] = value;
      const desired = dashboardLanguage === 'pt-BR' ? translateKnown(originals[attr]) : originals[attr];
      if (value !== desired) el.setAttribute(attr, desired);
    });
  });
}

function setDashboardLanguage(language) {
  dashboardLanguage = language === 'pt-BR' ? 'pt-BR' : 'en';
  try { localStorage.setItem(DASHBOARD_LANG_KEY, dashboardLanguage); } catch { /* private mode */ }
  applyDashboardLanguage();
  // Re-render the current tab so dates and dynamic interface labels follow suit.
  if (typeof loadCurrentTab === 'function') loadCurrentTab();
}

try { dashboardLanguage = localStorage.getItem(DASHBOARD_LANG_KEY) === 'pt-BR' ? 'pt-BR' : 'en'; }
catch { /* private mode */ }
applyDashboardLanguage();
let localeRefreshQueued = false;
new MutationObserver(() => {
  if (localeRefreshQueued) return;
  localeRefreshQueued = true;
  queueMicrotask(() => { localeRefreshQueued = false; applyDashboardLanguage(); });
}).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true,
                           attributeFilter: TRANSLATED_ATTRIBUTES});
