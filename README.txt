BETLOG — registo de apostas da equipa
=====================================

CORRER NO PC (testar):
1. Abre terminal na pasta betlog.
2. py -m pip install Flask werkzeug
3. py app.py
4. No browser: http://localhost:8001
5. Entra com:  utilizador = admin   palavra-passe = admin123
   (muda a palavra-passe no botão "Senha" depois de entrar)

CRIAR UTILIZADORES PARA A EQUIPA:
- Entra como admin -> "+ Novo utilizador" -> escolhe nome e palavra-passe.
- Dá esses dados a cada pessoa da equipa.

COMO FUNCIONA:
- Cada pessoa entra com o seu login e regista operações.
- Uma operação tem 2-3 pernas (casa + resultado + valor + odd).
- Marca-se qual perna ganhou; a app calcula o lucro/prejuízo.
- Cada pessoa vê só as suas; o admin vê tudo, com totais por pessoa.

PÔR ONLINE (Railway) — quando quiseres que a equipa aceda de fora:
1. Cria um projeto no Railway, liga este repositório.
2. Adiciona um Postgres (o Railway dá a variável DATABASE_URL automaticamente).
3. Define variáveis: SECRET_KEY (texto aleatório longo),
   ADMIN_USER e ADMIN_PASS (o teu login inicial).
4. O Railway usa o Procfile e arranca com gunicorn.
5. A app deteta o DATABASE_URL e usa Postgres em vez de SQLite.
IMPORTANTE: em produção, muda SECRET_KEY e a palavra-passe do admin.
