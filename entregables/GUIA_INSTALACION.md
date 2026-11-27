# Sellbird para Everest Design: cómo dejarlo corriendo en tu computadora

Todo lo de la configuración ya está hecho: el entrenamiento con everestdesign.uy, Uruguay como mercado, emails en español con voseo, la firma de Josema y la oferta del mockup gratis de la home.
Falta instalarlo en una máquina que quede encendida, porque tiene que mandar emails y leer respuestas todos los días.

## 1. Requisitos

- Python 3.11 o más nuevo. En Mac: `brew install python@3.13`
- Claude Code con tu plan Pro o Max, logueado (`claude login`)
- Una casilla para enviar, idealmente Google Workspace en un **dominio secundario**, por ejemplo `everestdesign-uy.com`. No uses `hola@everestdesign.uy` para emails en frío: si cae en spam, se te cae también el correo principal.
- Calentar esa casilla 2 a 4 semanas antes de empezar. Sellbird recomienda TrulyInbox.
- Un verificador de emails. Reoon da 600 verificaciones gratis por mes. Sin verificador no se envía nada.

## 2. Instalar

```bash
git clone https://github.com/Sh1tH4ppens/sellbird.git
```

```bash
cd sellbird && git checkout everest-uruguay
```

```bash
python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Si no tenés el fork, cloná `https://github.com/ethanplusai/sellbird.git` y copiá el archivo `sellbird/collectors/discover.py` de esta carpeta.

## 3. Copiar la configuración de Everest

Copiá estos archivos de la carpeta `config_everest/` a la carpeta de sellbird:

| Archivo | Destino |
|---|---|
| `sellbird.local.yaml` | `sellbird/sellbird.local.yaml` |
| `product_knowledge.md` | `sellbird/skills/product_knowledge.md` |
| `competitive_intel.md` | `sellbird/skills/competitive_intel.md` |
| `sellbird.db` | `sellbird/data/sellbird.db` (los negocios que ya encontramos) |

## 4. Configurar la casilla y el verificador

```bash
cp .env.example .env
```

En `.env` completá `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` y `REOON_API_KEY`. Después:

```bash
sellbird gmail auth
```

```bash
sellbird gmail test
```

En `sellbird.local.yaml` cambiá `persona.email` por la casilla de envío y poné en `channels.email.warmup.start_date` la fecha en que empezaste a calentarla.

## 5. Arrancar

```bash
sellbird dashboard
```

```bash
sellbird run
```

El dashboard abre en un link privado (`http://127.0.0.1:5555/#token=...`). En **Outbound → Waiting for you** vas a ver cada email con el motivo por el que se eligió a esa persona. Nada sale sin que aprietes **Approve**.

Para frenar todo de golpe:

```bash
sellbird sending pause
```

## 6. Conectarlo a Claude

```bash
sellbird connect
```

Después podés preguntarle a Claude "¿Qué necesita Sellbird de mí?" o "¿Qué hizo Sellbird anoche?".

## Qué está configurado

- **Rubros:** ferreterías, ropa, electrodomésticos, panaderías, contadores, abogados, dentistas, clínicas, inmobiliarias, restaurantes, cafeterías, hoteles y distribuidoras mayoristas
- **Ciudades:** Montevideo, Punta del Este, Ciudad de la Costa, Colonia del Sacramento, Salto y Paysandú
- **Horario:** no envía entre las 20:00 y las 08:00, hora de Montevideo
- **Límite:** 30 emails por día
- **Oferta:** un mockup gratis de su página principal rediseñada
- **Aprobación manual:** activada

## Ojo con

- Alrededor de Salto y Paysandú aparecen negocios de Argentina (Concordia, Villa Elisa). Si no querés incluirlos, sacalos de la lista o agregá coordenadas con un radio más chico en `icp.geo_coordinates`, por ejemplo `"Salto, Uruguay": "-31.3833,-57.9667,10"`.
- La mayoría de los negocios de OpenStreetMap no tienen web cargada. Para esos, usá el archivo `prospectos_whatsapp_uruguay.csv` y escribiles por WhatsApp.
- Si OpenStreetMap se queda corto, DataForSEO Business Listings cuesta unos USD 0,37 cada 1.000 negocios y trae teléfono, web y reseñas de Google.
