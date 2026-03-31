import discord
from discord.ext import commands
from discord import app_commands
from typing import List, Optional
import json
import os
import re
import io
import qrcode
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════
#  CONFIGURAÇÕES
# ══════════════════════════════════════════════════════
TOKEN        = os.getenv("TOKEN", "SEU_TOKEN_AQUI")
CHAVE_PIX    = os.getenv("CHAVE_PIX", "sua@chave.pix")

# Obrigatórios pelo padrão EMV do PIX (Banco Central) — pode deixar genérico
NOME_RECEBEDOR   = "Loja"
CIDADE_RECEBEDOR = "Brasil"

CATEGORIA_CARRINHOS_ID = 0            # ID da categoria onde os canais de carrinho serão criados
                                       # (0 = sem categoria)

PRODUTOS_FILE = "produtos.json"
CUPONS_FILE   = "cupons.json"

# ══════════════════════════════════════════════════════
#  GERADOR DE PIX COPIA E COLA (padrão EMV)
# ══════════════════════════════════════════════════════
def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"

def gerar_pix_copia_cola(chave: str, nome: str, cidade: str, valor: float, txid: str = "***") -> str:
    valor_str = f"{valor:.2f}"

    merchant_account = (
        _tlv("00", "BR.GOV.BCB.PIX") +
        _tlv("01", chave)
    )

    payload = (
        _tlv("00", "01") +
        _tlv("26", merchant_account) +
        _tlv("52", "0000") +
        _tlv("53", "986") +
        _tlv("54", valor_str) +
        _tlv("58", "BR") +
        _tlv("59", nome[:25]) +
        _tlv("60", cidade[:15]) +
        _tlv("62", _tlv("05", txid[:25]))
    )
    payload += "6304"  # CRC tag + tamanho fixo (4 chars)

    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
            crc &= 0xFFFF

    return payload[:-4] + f"6304{crc:04X}"

# ══════════════════════════════════════════════════════
#  GERADOR DE QR CODE PIX
# ══════════════════════════════════════════════════════
def gerar_qr_bytes(pix_str: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(pix_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# ══════════════════════════════════════════════════════
#  PERSISTÊNCIA
# ══════════════════════════════════════════════════════
def carregar_produtos() -> List[dict]:
    if os.path.exists(PRODUTOS_FILE):
        with open(PRODUTOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return [
        {
            "id": "card_ativacao",
            "nome": "Card de Ativação",
            "descricao": (
                "Ativação de 3-4 contas sem VBV e preço menor do mercado. "
                "**Não é para realizar compras com o card, pois será bloqueado.**\n\n"
                "*Garantimos a melhor qualidade no nosso card*\n"
                "*Card somente para a ativação de Nitro*"
            ),
            "entrega": "Entrega Automática!",
            "preco": "R$ 2,00",
            "valor": 2.00,
            "cor": 0x7C3AED,
        },
    ]

def salvar_produtos():
    with open(PRODUTOS_FILE, "w", encoding="utf-8") as f:
        json.dump(PRODUTOS, f, ensure_ascii=False, indent=2)

def carregar_cupons() -> dict:
    if os.path.exists(CUPONS_FILE):
        with open(CUPONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def salvar_cupons():
    with open(CUPONS_FILE, "w", encoding="utf-8") as f:
        json.dump(CUPONS, f, ensure_ascii=False, indent=2)

def gerar_id(nome: str) -> str:
    base = re.sub(r"[^a-z0-9]", "_", nome.lower().strip())
    base = re.sub(r"_+", "_", base).strip("_")
    ids = {p["id"] for p in PRODUTOS}
    if base not in ids:
        return base
    i = 2
    while f"{base}_{i}" in ids:
        i += 1
    return f"{base}_{i}"

def extrair_valor(preco_str: str) -> float:
    nums = re.sub(r"[^\d,.]", "", preco_str).replace(",", ".")
    try:
        return float(nums)
    except ValueError:
        return 0.0

PRODUTOS: List[dict] = carregar_produtos()
CUPONS:   dict       = carregar_cupons()

# contador de carrinhos
_carrinhos_file = "carrinhos.json"
def _proximo_carrinho() -> int:
    data = {}
    if os.path.exists(_carrinhos_file):
        with open(_carrinhos_file) as f:
            data = json.load(f)
    n = data.get("ultimo", 0) + 1
    data["ultimo"] = n
    with open(_carrinhos_file, "w") as f:
        json.dump(data, f)
    return n

# ══════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

COR_MAP = {
    "roxo": 0x7C3AED, "azul": 0x5865F2, "verde": 0x1D9E75,
    "vermelho": 0xE74C3C, "laranja": 0xE67E22, "rosa": 0xE91E8C,
    "amarelo": 0xF1C40F, "cinza": 0x95A5A6,
}

# ══════════════════════════════════════════════════════
#  CARRINHO — View principal
# ══════════════════════════════════════════════════════
class ViewCarrinho(discord.ui.View):
    def __init__(self, produto: dict, numero: int, cupom_aplicado: Optional[dict] = None):
        super().__init__(timeout=None)
        self.produto        = produto
        self.numero         = numero
        self.cupom_aplicado = cupom_aplicado

    def calcular_total(self) -> float:
        base = self.produto.get("valor") or extrair_valor(self.produto["preco"])
        if self.cupom_aplicado:
            desc = self.cupom_aplicado.get("desconto", 0)
            base = max(0.0, base - (base * desc / 100))
        return round(base, 2)

    def build_embed(self, usuario: discord.Member) -> discord.Embed:
        total = self.calcular_total()
        base  = self.produto.get("valor") or extrair_valor(self.produto["preco"])

        embed = discord.Embed(
            title=f"🛒 Carrinho #{self.numero:03d}",
            color=self.produto.get("cor", 0x7C3AED),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Produto",  value=self.produto["nome"], inline=True)
        embed.add_field(name="Preço",    value=f"R$ {base:.2f}",     inline=True)

        if self.cupom_aplicado:
            desc = self.cupom_aplicado["desconto"]
            embed.add_field(
                name="🎟️ Cupom",
                value=f"`{self.cupom_aplicado['codigo']}` — {desc}% de desconto",
                inline=False,
            )
            embed.add_field(name="💰 Total", value=f"**R$ {total:.2f}**", inline=True)
        else:
            embed.add_field(name="💰 Total", value=f"**R$ {total:.2f}**", inline=True)

        embed.set_footer(text="Use os botões abaixo para continuar ou cancelar.")
        return embed

    @discord.ui.button(label="Adicionar Cupom", style=discord.ButtonStyle.secondary, emoji="🎟️", row=0)
    async def btn_cupom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalCupom(self))

    @discord.ui.button(label="Prosseguir Pagamento", style=discord.ButtonStyle.success, emoji="💳", row=0)
    async def btn_prosseguir(self, interaction: discord.Interaction, button: discord.ui.Button):
        canal = interaction.channel
        usuario = interaction.user

        # Renomeia canal para pagamento-NNN-usuario
        novo_nome = f"pagamento-{self.numero:03d}-{usuario.name}"
        try:
            await canal.edit(name=novo_nome)
        except discord.Forbidden:
            pass

        total = self.calcular_total()
        txid  = f"CARR{self.numero:03d}"
        pix   = gerar_pix_copia_cola(CHAVE_PIX, NOME_RECEBEDOR, CIDADE_RECEBEDOR, total, txid)

        # Deleta embed antiga
        await interaction.message.delete()

        qr_buf = gerar_qr_bytes(pix)
        qr_file = discord.File(qr_buf, filename="qrcode.png")

        embed_pag = discord.Embed(
            title=f"💳 Pagamento #{self.numero:03d}",
            description=(
                f"Olá {usuario.mention}!\n\n"
                f"**Produto:** {self.produto['nome']}\n"
                f"**Total: R$ {total:.2f}**\n\n"
                "Pague via PIX usando o QR Code ou o código abaixo:"
            ),
            color=0x1D9E75,
        )
        embed_pag.add_field(
            name="📋 Copia e Cola",
            value=f"```\n{pix}\n```",
            inline=False,
        )
        embed_pag.set_image(url="attachment://qrcode.png")
        embed_pag.set_footer(text="Após o pagamento, aguarde a confirmação da equipe.")

        await canal.send(
            content=usuario.mention,
            embed=embed_pag,
            file=qr_file,
            view=ViewPagamento(),
        )
        await interaction.response.defer()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌", row=0)
    async def btn_cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Cancelando carrinho...", ephemeral=True)
        await interaction.channel.delete()


# ── Modal de cupom ──
class ModalCupom(discord.ui.Modal, title="Adicionar Cupom"):
    codigo = discord.ui.TextInput(
        label="Código do cupom",
        placeholder="Ex: DESCONTO10",
        max_length=30,
    )

    def __init__(self, view_carrinho: ViewCarrinho):
        super().__init__()
        self.view_carrinho = view_carrinho

    async def on_submit(self, interaction: discord.Interaction):
        codigo = self.codigo.value.strip().upper()
        cupom  = CUPONS.get(codigo)

        if not cupom:
            await interaction.response.send_message(
                f"❌ Cupom `{codigo}` não encontrado.", ephemeral=True
            )
            return

        self.view_carrinho.cupom_aplicado = {**cupom, "codigo": codigo}
        embed = self.view_carrinho.build_embed(interaction.user)
        await interaction.response.edit_message(embed=embed, view=self.view_carrinho)


# ── View de pagamento (copia e cola + qr) ──
class ViewPagamento(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Já Paguei", style=discord.ButtonStyle.success, emoji="✅")
    async def btn_pago(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "✅ Pagamento informado! Aguarde a confirmação da equipe.",
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════
#  PAINEL DE VENDAS — dropdown que abre carrinho
# ══════════════════════════════════════════════════════
class DropdownProdutos(discord.ui.Select):
    def __init__(self, produtos_selecionados: List[dict]):
        options = [
            discord.SelectOption(
                label=p["nome"],
                value=p["id"],
                description=p["preco"],
                emoji="🛒",
            )
            for p in produtos_selecionados
        ]
        super().__init__(
            placeholder="Selecione um produto...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dropdown_produtos",
        )

    async def callback(self, interaction: discord.Interaction):
        produto_id = self.values[0]
        produto    = next((p for p in PRODUTOS if p["id"] == produto_id), None)
        if not produto:
            await interaction.response.send_message("❌ Produto não encontrado.", ephemeral=True)
            return

        guild   = interaction.guild
        usuario = interaction.user
        numero  = _proximo_carrinho()

        # Permissões: só o usuário e o bot enxergam
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            usuario:            discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }

        categoria = None
        if CATEGORIA_CARRINHOS_ID:
            categoria = guild.get_channel(CATEGORIA_CARRINHOS_ID)

        canal = await guild.create_text_channel(
            name=f"carrinho-{numero:03d}-{usuario.name}",
            overwrites=overwrites,
            category=categoria,
            reason=f"Carrinho #{numero:03d} de {usuario}",
        )

        view  = ViewCarrinho(produto, numero)
        embed = view.build_embed(usuario)

        await canal.send(content=usuario.mention, embed=embed, view=view)
        await interaction.response.send_message(
            f"🛒 Seu carrinho foi criado! Acesse {canal.mention}", ephemeral=True
        )


class PainelView(discord.ui.View):
    def __init__(self, produtos_selecionados: List[dict]):
        super().__init__(timeout=None)
        self.add_item(DropdownProdutos(produtos_selecionados))


# ══════════════════════════════════════════════════════
#  /setup — wizard 2 etapas
# ══════════════════════════════════════════════════════
class DropdownEscolherProdutos(discord.ui.Select):
    def __init__(self, canal_destino: discord.TextChannel, titulo: str, texto: str):
        self.canal_destino = canal_destino
        self.titulo        = titulo
        self.texto         = texto
        options = [
            discord.SelectOption(label=p["nome"], value=p["id"], description=p["preco"])
            for p in PRODUTOS
        ]
        super().__init__(
            placeholder="Escolha os produtos do painel...",
            min_values=1,
            max_values=len(PRODUTOS),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        produtos_escolhidos = [p for p in PRODUTOS if p["id"] in self.values]
        cor   = produtos_escolhidos[0]["cor"] if produtos_escolhidos else 0x7C3AED
        embed = discord.Embed(title=self.titulo, description=self.texto, color=cor)
        embed.set_footer(text="Selecione um produto no menu abaixo para comprar.")
        await self.canal_destino.send(embed=embed, view=PainelView(produtos_escolhidos))
        await interaction.response.edit_message(
            content=f"✅ Painel enviado em {self.canal_destino.mention}!",
            embed=None, view=None,
        )

class ViewEscolherProdutos(discord.ui.View):
    def __init__(self, canal_destino, titulo, texto):
        super().__init__(timeout=120)
        self.add_item(DropdownEscolherProdutos(canal_destino, titulo, texto))

class ModalSetup(discord.ui.Modal, title="Configurar Painel"):
    titulo   = discord.ui.TextInput(label="Título do painel", placeholder="Ex: 🛒 Loja", max_length=100)
    texto    = discord.ui.TextInput(label="Texto / descrição", style=discord.TextStyle.paragraph, max_length=1000)
    canal_id = discord.ui.TextInput(label="ID do canal de destino", max_length=25)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            canal = interaction.guild.get_channel(int(self.canal_id.value.strip()))
        except ValueError:
            canal = None
        if not isinstance(canal, discord.TextChannel):
            await interaction.response.send_message("❌ Canal não encontrado.", ephemeral=True)
            return
        if not PRODUTOS:
            await interaction.response.send_message("❌ Nenhum produto cadastrado ainda.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Passo 2 — Escolha os produtos",
            description=f"Painel será enviado em {canal.mention}.\nEscolha os produtos:",
            color=0x7C3AED,
        )
        await interaction.response.send_message(
            embed=embed,
            view=ViewEscolherProdutos(canal, self.titulo.value, self.texto.value),
            ephemeral=True,
        )

@tree.command(name="setup", description="Cria e envia um painel de vendas")
@app_commands.default_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    await interaction.response.send_modal(ModalSetup())


# ══════════════════════════════════════════════════════
#  CRIAR PRODUTO
# ══════════════════════════════════════════════════════
class ModalCriarProduto(discord.ui.Modal, title="Criar Produto"):
    nome      = discord.ui.TextInput(label="Nome do produto", max_length=80)
    preco     = discord.ui.TextInput(label="Preço (ex: R$ 2,00)", max_length=30)
    descricao = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=800)
    entrega   = discord.ui.TextInput(label="Tipo de entrega", default="Entrega Automática!", max_length=60)
    cor       = discord.ui.TextInput(label="Cor (roxo|azul|verde|vermelho|laranja|rosa|amarelo|cinza)", default="roxo", max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        cor_hex = COR_MAP.get(self.cor.value.strip().lower(), 0x7C3AED)
        pid     = gerar_id(self.nome.value)
        valor   = extrair_valor(self.preco.value)
        novo = {
            "id":       pid,
            "nome":     self.nome.value.strip(),
            "descricao":self.descricao.value.strip(),
            "entrega":  self.entrega.value.strip(),
            "preco":    self.preco.value.strip(),
            "valor":    valor,
            "cor":      cor_hex,
        }
        PRODUTOS.append(novo)
        salvar_produtos()
        embed = discord.Embed(title="✅ Produto criado!", color=cor_hex)
        embed.add_field(name="Nome",      value=novo["nome"],          inline=True)
        embed.add_field(name="Preço",     value=novo["preco"],         inline=True)
        embed.add_field(name="Entrega",   value=f"⚡ {novo['entrega']}", inline=True)
        embed.add_field(name="Descrição", value=novo["descricao"],     inline=False)
        embed.set_footer(text=f"ID: {pid}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════
#  CRIAR CUPOM
# ══════════════════════════════════════════════════════
class ModalCriarCupom(discord.ui.Modal, title="Criar Cupom"):
    codigo   = discord.ui.TextInput(label="Código do cupom", placeholder="Ex: PROMO20", max_length=30)
    desconto = discord.ui.TextInput(label="Desconto (%)", placeholder="Ex: 10  (para 10%)", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        cod = self.codigo.value.strip().upper()
        try:
            pct = float(self.desconto.value.strip().replace(",", "."))
            assert 0 < pct <= 100
        except (ValueError, AssertionError):
            await interaction.response.send_message("❌ Desconto inválido. Use um número entre 1 e 100.", ephemeral=True)
            return

        CUPONS[cod] = {"desconto": pct}
        salvar_cupons()

        embed = discord.Embed(title="✅ Cupom criado!", color=0x1D9E75)
        embed.add_field(name="Código",   value=f"`{cod}`",    inline=True)
        embed.add_field(name="Desconto", value=f"{pct:.0f}%", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════
#  /setupadm
# ══════════════════════════════════════════════════════
class DropdownAdm(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Selecione uma ação...",
            custom_id="dropdown_adm",
            options=[
                discord.SelectOption(label="Criar Produto", value="criar_produto", description="Adiciona um novo produto", emoji="📦"),
                discord.SelectOption(label="Criar Cupom",   value="criar_cupom",   description="Cria um cupom de desconto", emoji="🎟️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "criar_produto":
            await interaction.response.send_modal(ModalCriarProduto())
        else:
            await interaction.response.send_modal(ModalCriarCupom())

class PainelAdmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(DropdownAdm())

@tree.command(name="setupadm", description="Abre o painel administrativo da loja")
@app_commands.default_permissions(administrator=True)
async def cmd_setupadm(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ Painel Administrativo", color=0x5865F2,
        description="Selecione uma opção no menu abaixo.")
    embed.add_field(name="📦 Criar Produto", value="Adiciona um novo produto à loja",  inline=False)
    embed.add_field(name="🎟️ Criar Cupom",   value="Cria um cupom de desconto",        inline=False)
    embed.set_footer(text=f"{len(PRODUTOS)} produto(s) · {len(CUPONS)} cupom(ns) · Só admins")
    await interaction.response.send_message(embed=embed, view=PainelAdmView(), ephemeral=True)


# ══════════════════════════════════════════════════════
#  EVENTOS
# ══════════════════════════════════════════════════════
@bot.event
async def on_ready():
    await tree.sync()
    bot.add_view(PainelAdmView())
    print(f"✅ Bot online como {bot.user}")
    print(f"   {len(PRODUTOS)} produto(s) | {len(CUPONS)} cupom(ns)")


bot.run(TOKEN)
