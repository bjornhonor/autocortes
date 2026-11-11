# Publicação: o que cada plataforma exige

Conferido nas documentações oficiais em 11/11/2026. Essas regras mudam com frequência; revise antes de implementar cada publicador (fase 4).

## Resumo

| | YouTube Shorts | Instagram Reels | TikTok |
|---|---|---|---|
| API | YouTube Data API v3, `videos.insert` | Instagram Graph API, container `REELS` | Content Posting API, Direct Post |
| Conta | qualquer canal | profissional (Creator ou Business) | qualquer, mas veja a auditoria |
| Autorização | OAuth 2.0 (Google Cloud) | Business Login for Instagram ou Facebook Login for Business | Login Kit (OAuth 2.0) |
| Limite | 100 uploads/dia (cota própria de uploads) | 100 posts via API por 24 h, por conta | teto diário por criador (~15); 6 req/min por token |
| Público sem auditoria? | sim | sim, na sua conta, com o app em modo desenvolvimento | **não**: tudo sai privado até o app ser auditado |
| Envio do vídeo | arquivo local, upload resumível | URL pública, ou upload resumível (este só com Facebook Login for Business) | arquivo local em pedaços, ou URL de domínio verificado |

## YouTube

Configuração:
1. Google Cloud Console → novo projeto → ativar a **YouTube Data API v3**.
2. Tela de consentimento OAuth (tipo externo) e credencial **"App para computador"**; baixar o JSON para `secrets/youtube_client_secret.json` (caminho em `YOUTUBE_CLIENT_SECRETS`).
3. Escopo `https://www.googleapis.com/auth/youtube.upload`. Na primeira vez abre o navegador para autorizar; o refresh token fica salvo em `secrets/`.

Pegadinhas:
- Com a tela de consentimento em modo **Teste**, o refresh token expira em 7 dias. Para rodar direto, mude para **Em produção**; sem verificação o Google mostra o aviso de "app não verificado", mas para uso próprio funciona.
- Vídeo vertical ou quadrado de até 3 min vira Short automaticamente; `#Shorts` no título é opcional.
- Sem `privacyStatus` o vídeo sobe **público**. Nos testes, use `private` ou `unlisted`.
- Mudou: antes cada upload custava 1.600 das 10.000 unidades diárias (~6 vídeos/dia) e projetos não verificados só subiam vídeo privado. A documentação atual dá ao upload uma cota própria (1 unidade, 100 chamadas/dia) e diz que projetos não verificados não ficam mais restritos a privado. Muito tutorial na internet ainda cita as regras antigas.

## Instagram

Configuração:
1. Converter a conta para **profissional** (Creator ou Business).
2. developers.facebook.com → criar app → adicionar o produto Instagram.
3. Escolher o login:
   - **Business Login for Instagram** (`graph.instagram.com`): mais simples, não precisa de Página do Facebook. Permissões `instagram_business_basic` + `instagram_business_content_publish`. O vídeo precisa estar numa **URL pública**.
   - **Facebook Login for Business** (`graph.facebook.com`): exige uma Página do Facebook ligada à conta. Permissões `instagram_basic` + `instagram_content_publish` + `pages_read_engagement`. Permite **upload resumível** do arquivo local via `rupload.facebook.com`.
4. Para postar só na sua conta, o app em modo desenvolvimento (você com papel no app) deve bastar, sem App Review. Confirmar na fase 4.

Fluxo: `POST /<IG_ID>/media` com `media_type=REELS` (+ `video_url`, ou `upload_type=resumable` seguido do upload) → `GET /<CONTAINER_ID>?fields=status_code` até `FINISHED` → `POST /<IG_ID>/media_publish` com `creation_id=<CONTAINER_ID>`.

Pegadinhas:
- Container não publicado em 24 h expira.
- O token de longa duração vale 60 dias e precisa ser renovado antes de vencer.
- Limite de 100 posts via API por 24 h (janela móvel); `GET /<IG_ID>/content_publishing_limit` mostra o uso.
- URL pública sem servidor próprio: subir para um bucket (Cloudflare R2, S3) com link temporário, ou abrir um túnel (cloudflared) só durante o upload.

## TikTok — o gargalo do projeto

Configuração:
1. developers.tiktok.com → criar app → adicionar **Login Kit** e **Content Posting API** com **Direct Post** ativado.
2. Aprovação e autorização do escopo `video.publish`.

Fluxo: `POST /v2/post/publish/creator_info/query/` (opções de privacidade que o criador permite) → `POST /v2/post/publish/video/init/` com `post_info` (`privacy_level` obrigatório e dentro dessas opções; `title`) e `source_info` (`FILE_UPLOAD` com `video_size`, `chunk_size`, `total_chunk_count`) → `PUT` de cada pedaço no `upload_url` com `Content-Range` → `POST /v2/post/publish/status/fetch/` até publicar.

Pegadinhas:
- **App não auditado**: todo post sai privado, e só dá para postar em **conta privada**. Com conta pública, o `video/init` devolve 403 `unaudited_client_can_only_post_to_private_accounts`.
- Para postar público, o app precisa passar na **auditoria** da Content Posting API, que avalia o caso de uso e a tela de postagem (o usuário precisa ver e escolher privacidade, título etc.). Comece esse processo cedo.
- Limites: 6 requisições/min por token; teto diário de posts por criador, em geral ~15, somando todos os apps (`spam_risk_too_many_posts`); teto de usuários ativos por app (`reached_active_user_cap`).
- O access token dura 24 h; renove com o refresh token.

Plano B enquanto a auditoria não sai: mandar o vídeo para os **rascunhos** do app do TikTok (endpoint de inbox: o criador publica no celular com um toque; confirmar as regras para apps não auditados) ou usar um agregador pago que já é auditado (ex.: Ayrshare, Upload-Post), que publica nas três redes com uma API só.

## Fontes

- [YouTube Data API — Videos: insert](https://developers.google.com/youtube/v3/docs/videos/insert)
- [Instagram Platform — Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- [TikTok — Content Posting API: Get Started](https://developers.tiktok.com/doc/content-posting-api-get-started)
- [TikTok — Direct Post API reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post)
- [TikTok — Content Sharing Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
