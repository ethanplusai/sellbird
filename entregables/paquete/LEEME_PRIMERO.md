# Sellbird para Everest Design: guía paso a paso

No necesitás saber usar GitHub. Son 6 pasos y la primera vez lleva alrededor de una hora.

## Qué hay en este ZIP

| Archivo | Qué es |
|---|---|
| `sellbird.local.yaml` | Toda la configuración de Everest: servicios, precios, rubros, ciudades, oferta del mockup, firma de Josema |
| `product_knowledge.md` | Lo que Sellbird aprendió de everestdesign.uy |
| `competitive_intel.md` | Tus competidores y cómo diferenciarte |
| `sellbird.db` | Los 1.863 negocios que ya encontramos |
| `env.txt` | Las claves de tu casilla de email y del verificador. Lo completás en el paso 4 |
| `prospectos_whatsapp_uruguay.csv` | 308 negocios con link a WhatsApp. Lo podés usar hoy mismo, se abre con Excel |

---

## Paso 1. Crear la casilla de envío (una sola vez)

**No uses `hola@everestdesign.uy` para emails en frío.** Si Google lo marca como spam, te quedás sin tu correo principal.

1. Comprá un dominio parecido, por ejemplo `everestdesign-uy.com` o `everestdesignuy.com`, unos USD 12 por año.
2. Contratá Google Workspace para ese dominio, unos USD 7 por mes, y creá la casilla `josema@ese-dominio`.
3. **Calentala 2 a 4 semanas** antes de mandar emails en frío. Un servicio como TrulyInbox manda y responde emails automáticamente para que Google confíe en la casilla.

## Paso 2. Sacar las dos claves (una sola vez)

**a) Contraseña de aplicación de Google.** Es una contraseña especial para que Sellbird pueda enviar desde la casilla.

1. Entrá a https://myaccount.google.com con la casilla nueva.
2. En **Seguridad**, activá la **Verificación en 2 pasos**.
3. Entrá a https://myaccount.google.com/apppasswords, escribí "Sellbird" y apretá **Crear**.
4. Copiá la contraseña de 16 letras que aparece.

**b) Verificador de emails, Reoon.** Sin esta clave Sellbird no envía nada, porque no puede comprobar que los emails existan.

1. Creá una cuenta gratis en https://www.reoon.com/email-verifier/. Incluye 600 verificaciones por mes.
2. En el panel buscá **API** y copiá tu clave.

## Paso 3. Instalar los programas (una sola vez)

1. **Python:** bajalo de https://www.python.org/downloads/ e instalalo. En Windows, marcá la casilla **"Add Python to PATH"**.
2. **Claude Code:** seguí las instrucciones de https://claude.com/claude-code e iniciá sesión con tu cuenta de Claude. Sellbird usa tu plan de Claude para escribir los emails, sin costo extra.
3. **Sellbird con la configuración de Everest:** descargá este ZIP y descomprimilo, por ejemplo en Documentos.
   **https://github.com/Sh1tH4ppens/sellbird/archive/refs/heads/everest-uruguay.zip**
   Te va a quedar una carpeta llamada `sellbird-everest-uruguay`.

## Paso 4. Copiar los archivos de este paquete

Dentro de la carpeta `sellbird-everest-uruguay`:

| Archivo de este paquete | Dónde va |
|---|---|
| `sellbird.local.yaml` | En la carpeta principal `sellbird-everest-uruguay` |
| `product_knowledge.md` y `competitive_intel.md` | Dentro de la subcarpeta `skills` (reemplazá los que haya) |
| `sellbird.db` | Creá una subcarpeta llamada `data` y ponelo adentro |
| `env.txt` | En la carpeta principal, **renombrado a `.env`** (con el punto adelante y sin `.txt`) |

Después abrí `.env` con el Bloc de notas (Windows) o TextEdit (Mac) y completalo:

```
SMTP_USERNAME=josema@tu-dominio-nuevo.com
SMTP_PASSWORD=la contraseña de 16 letras del paso 2a
IMAP_USERNAME=josema@tu-dominio-nuevo.com
IMAP_PASSWORD=la misma contraseña de 16 letras
REOON_API_KEY=la clave de Reoon del paso 2b
```

Por último, abrí `sellbird.local.yaml` y en la línea `email: hola@everestdesign.uy` poné la casilla nueva.

## Paso 5. Instalar Sellbird (una sola vez)

Abrí la terminal dentro de la carpeta `sellbird-everest-uruguay`:

- **Mac:** clic derecho sobre la carpeta → **Servicios** → **Nueva terminal en la carpeta**
- **Windows:** abrí la carpeta, escribí `cmd` en la barra de direcciones y apretá Enter

Pegá esto y apretá Enter.

En Mac:

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

En Windows:

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -e .
```

Tarda unos minutos.

## Paso 6. Usarlo todos los días

Cada vez que quieras usarlo, abrí la terminal en la carpeta como en el paso 5.

Primero activá Sellbird.

En Mac:

```bash
source .venv/bin/activate
```

En Windows:

```bash
.venv\Scripts\activate
```

Abrí el panel de control:

```bash
sellbird dashboard
```

Te muestra un link. Abrilo en el navegador.

En **otra** terminal (activala de nuevo con el comando de arriba), arrancá el trabajo automático:

```bash
sellbird run
```

Dejá la computadora prendida. Cada 15 minutos Sellbird lee las webs de los negocios, busca al dueño, verifica su email y escribe un borrador.

**Nada se envía sin tu OK.** En el panel, entrá a **Outbound → Waiting for you**. Cada email muestra por qué se eligió a esa persona. Leelo, corregilo si querés y apretá **Approve**.

Para frenar todo de golpe:

```bash
sellbird sending pause
```

---

## Cómo quedó configurado

- **Rubros:** ferreterías, ropa, electrodomésticos, panaderías, contadores, abogados, dentistas, clínicas, inmobiliarias, restaurantes, cafeterías, hoteles y mayoristas
- **Ciudades:** Montevideo, Punta del Este, Ciudad de la Costa, Colonia, Salto y Paysandú
- **Horario:** envía de 8:00 a 20:00, hora de Uruguay
- **Límite:** hasta 30 emails por día. Las primeras semanas empieza con menos y va subiendo.
- **Oferta:** un mockup gratis de su página principal rediseñada
- **Firma:** Josema, Everest Design, con español rioplatense y voseo

## Si algo falla

Abrí Claude Code dentro de la carpeta `sellbird-everest-uruguay` (escribí `claude` en la terminal) y decile: **"ayudame con Sellbird"**. La carpeta trae instrucciones para que Claude te guíe paso a paso.
