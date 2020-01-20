# Como subir no Heroku através do Ubuntu 19.10

## Instalações necessárias
### pip3
sudo apt install python3-pip

### datasette
sudo pip3 install datasette
reiniciar o Ubuntu após instalação

### Heroku Cli
sudo snap install --classic heroku

## Subindo no Heroku
datasette publish heroku "diretório com os .dbs" -n "nome do app do Heroku" --extra-options="--config sql_time_limit_ms:60000 --config max_returned_rows:10000 --config force_https_urls:1" --template-dir "diretório com as páginas .html" -m "localização do metadata.json"

### Exemplo
datasette publish heroku ~/Dropbox/bases/* -n nucleo-teste --extra-options="--config sql_time_limit_ms:60000 --config max_returned_rows:10000 --config force_https_urls:1" --template-dir ~/Downloads/datasette-master/datasette_codes/templates -m ~/Downloads/datasette-master/metadata.json
