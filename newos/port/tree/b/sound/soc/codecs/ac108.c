// SPDX-License-Identifier: GPL-2.0
/*
 * ac108.c  --  ac108 ALSA Soc Audio driver
 *
 * Version: 2.0
 *
 * Author: panjunwen
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License version 2 as
 * published by the Free Software Foundation.
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/delay.h>
#include <linux/pm.h>
#include <linux/i2c.h>
#include <linux/kstrtox.h>
#include <linux/slab.h>
#include <linux/of.h>
#include <sound/core.h>
#include <sound/pcm.h>
#include <sound/pcm_params.h>
#include <sound/soc.h>
#include <sound/tlv.h>

#include "ac108.h"

/* test config */
#define AC108_ADC_PATTERN_SEL	ADC_PTN_NORMAL

/* AC108 config */
#define AC108_CHANNELS_MAX	8	/* range[1, 16] */
#define AC108_SLOT_WIDTH	32	/* 16bit or 32bit slot width */
#define AC108_DMIC_EN		0	/* 0:ADC  1:DMIC */
#define AC108_ENCODING_EN	0	/* TX Encoding mode enable */
#define AC108_ENCODING_CH_NUMS	8	/* TX Encoding channels [1, 16] */
#define AC108_LRCK_PERIOD	AC108_SLOT_WIDTH

#define AC108_SDO2_EN		1	/* AC108 SDO2/TX2 Enable */
#define AC108_IDLE_RESET_EN	0	/* reset AC108 when in idle time */

#define AC108_RATES		(SNDRV_PCM_RATE_8000_96000 | SNDRV_PCM_RATE_KNOT)
#define AC108_FORMATS		(SNDRV_PCM_FMTBIT_S16_LE | \
				 SNDRV_PCM_FMTBIT_S20_3LE | \
				 SNDRV_PCM_FMTBIT_S24_LE | \
				 SNDRV_PCM_FMTBIT_S32_LE)

static const DECLARE_TLV_DB_SCALE(adc_pga_gain_tlv, 0, 100, 0);
static const DECLARE_TLV_DB_SCALE(ch_digital_vol_tlv, -9750, 50, 1);

static struct i2c_client *i2c_clt[(AC108_CHANNELS_MAX + 3) / 4];

struct ac108_priv {
	struct i2c_client *i2c;
};


struct real_val_to_reg_val {
	unsigned int real_val;
	unsigned int reg_val;
};

struct pll_div {
	u32 freq_in;
	u32 freq_out;
	u32 m1;
	u32 m2;
	u32 n;
	u32 k1;
	u32 k2;
};



static const struct real_val_to_reg_val ac108_sample_rate[] = {
	{8000,  0},
	{11025, 1},
	{12000, 2},
	{16000, 3},
	{22050, 4},
	{24000, 5},
	{32000, 6},
	{44100, 7},
	{48000, 8},
	{96000, 9},
};

static const struct real_val_to_reg_val ac108_sample_resolution[] = {
	{8,  1},
	{12, 2},
	{16, 3},
	{20, 4},
	{24, 5},
	{28, 6},
	{32, 7},
};

static const struct real_val_to_reg_val ac108_bclk_div[] = {
	{0, 0},
	{1, 1},
	{2, 2},
	{4, 3},
	{6, 4},
	{8, 5},
	{12, 6},
	{16, 7},
	{24, 8},
	{32, 9},
	{48, 10},
	{64, 11},
	{96, 12},
	{128, 13},
	{176, 14},
	{192, 15},
};

/*
 * FOUT = (FIN * N) / [(M1+1) * (M2+1) * (K1+1) * (K2+1)]
 * M1[0,31], M2[0,1], N[0,1023], K1[0,31], K2[0,1]
 */
static const struct pll_div ac108_pll_div[] = {
	{400000,   24576000, 0,  0, 614, 4, 1},
	{512000,   24576000, 0,  0, 960, 9, 1},	/* 24576000/48 */
	{768000,   24576000, 0,  0, 640, 9, 1},	/* 24576000/32 */
	{800000,   24576000, 0,  0, 614, 9, 1},
	{1024000,  24576000, 0,  0, 480, 9, 1},	/* 24576000/24 */
	{1600000,  24576000, 0,  0, 307, 9, 1},
	{2048000,  24576000, 0,  0, 240, 9, 1},	/* 24576000/12 */
	{3072000,  24576000, 0,  0, 160, 9, 1},	/* 24576000/8 */
	{4096000,  24576000, 2,  0, 360, 9, 1},	/* 24576000/6 */
	{6000000,  24576000, 4,  0, 410, 9, 1},
	{12000000, 24576000, 9,  0, 410, 9, 1},
	{13000000, 24576000, 8,  0, 340, 9, 1},
	{15360000, 24576000, 12, 0, 415, 9, 1},
	{16000000, 24576000, 12, 0, 400, 9, 1},
	{19200000, 24576000, 15, 0, 410, 9, 1},
	{19680000, 24576000, 15, 0, 400, 9, 1},
	{24000000, 24576000, 9,  0, 205, 9, 1},

	{400000,   22579200, 0,  0, 566, 4, 1},
	{512000,   22579200, 0,  0, 880, 9, 1},
	{768000,   22579200, 0,  0, 587, 9, 1},
	{800000,   22579200, 0,  0, 567, 9, 1},
	{1024000,  22579200, 0,  0, 440, 9, 1},
	{1600000,  22579200, 1,  0, 567, 9, 1},
	{2048000,  22579200, 0,  0, 220, 9, 1},
	{3072000,  22579200, 0,  0, 148, 9, 1},
	{4096000,  22579200, 2,  0, 330, 9, 1},
	{6000000,  22579200, 2,  0, 227, 9, 1},
	{12000000, 22579200, 8,  0, 340, 9, 1},
	{13000000, 22579200, 9,  0, 350, 9, 1},
	{15360000, 22579200, 10, 0, 325, 9, 1},
	{16000000, 22579200, 11, 0, 340, 9, 1},
	{19200000, 22579200, 13, 0, 330, 9, 1},
	{19680000, 22579200, 14, 0, 345, 9, 1},
	{24000000, 22579200, 16, 0, 320, 9, 1},

	{12288000, 24576000, 9,  0, 400, 9, 1},	/* 24576000/2 */
	{11289600, 22579200, 9,  0, 400, 9, 1},	/* 22579200/2 */

	{24576000 / 1,   24576000, 9, 0, 200, 9, 1},	/* 24576000 */
	{24576000 / 4,   24576000, 4, 0, 400, 9, 1},	/* 6144000 */
	{24576000 / 16,  24576000, 0, 0, 320, 9, 1},	/* 1536000 */
	{24576000 / 64,  24576000, 0, 0, 640, 4, 1},	/* 384000 */
	{24576000 / 96,  24576000, 0, 0, 960, 4, 1},	/* 256000 */
	{24576000 / 128, 24576000, 0, 0, 512, 1, 1},	/* 192000 */
	{24576000 / 176, 24576000, 0, 0, 880, 4, 0},	/* 140000 */
	{24576000 / 192, 24576000, 0, 0, 960, 4, 0},	/* 128000 */

	{22579200 / 1,   22579200, 9, 0, 200, 9, 1},	/* 22579200 */
	{22579200 / 4,   22579200, 4, 0, 400, 9, 1},	/* 5644800 */
	{22579200 / 16,  22579200, 0, 0, 320, 9, 1},	/* 1411200 */
	{22579200 / 64,  22579200, 0, 0, 640, 4, 1},	/* 352800 */
	{22579200 / 96,  22579200, 0, 0, 960, 4, 1},	/* 235200 */
	{22579200 / 128, 22579200, 0, 0, 512, 1, 1},	/* 176400 */
	{22579200 / 176, 22579200, 0, 0, 880, 4, 0},	/* 128290 */
	{22579200 / 192, 22579200, 0, 0, 960, 4, 0},	/* 117600 */

	{22579200 / 6,   22579200, 2, 0, 360, 9, 1},	/* 3763200 */
	{22579200 / 8,   22579200, 0, 0, 160, 9, 1},	/* 2822400 */
	{22579200 / 12,  22579200, 0, 0, 240, 9, 1},	/* 1881600 */
	{22579200 / 24,  22579200, 0, 0, 480, 9, 1},	/* 940800 */
	{22579200 / 32,  22579200, 0, 0, 640, 9, 1},	/* 705600 */
	{22579200 / 48,  22579200, 0, 0, 960, 9, 1},	/* 470400 */
};



















static int ac108_read(u8 reg, u8 *rt_value, struct i2c_client *client)
{
	int ret;
	u8 read_cmd[1];

	if (!client || !client->adapter)
		return -ENODEV;

	read_cmd[0] = reg;

	ret = i2c_master_send(client, read_cmd, 1);
	if (ret != 1) {
		dev_err(&client->dev, "%s: write reg 0x%02x failed: %d\n",
			__func__, reg, ret);
		return ret < 0 ? ret : -EIO;
	}

	ret = i2c_master_recv(client, rt_value, 1);
	if (ret != 1) {
		dev_err(&client->dev, "%s: recv reg 0x%02x failed: %d\n",
			__func__, reg, ret);
		return ret < 0 ? ret : -EIO;
	}

	return 0;
}

static int ac108_write(u8 reg, unsigned char value, struct i2c_client *client)
{
	int ret;
	u8 write_cmd[2];

	if (!client || !client->adapter)
		return -ENODEV;

	write_cmd[0] = reg;
	write_cmd[1] = value;

	ret = i2c_master_send(client, write_cmd, 2);
	if (ret != 2) {
		dev_err(&client->dev,
			"%s: error->[REG-0x%02x,val-0x%02x]: %d\n",
			__func__, reg, value, ret);
		return ret < 0 ? ret : -EIO;
	}

	return 0;
}

static int ac108_soft_reset(struct i2c_client *client)
{
	u8 write_cmd[2] = { CHIP_AUDIO_RST, 0x12 };
	u8 val;
	int ret;

	if (!client || !client->adapter)
		return -ENODEV;

	/*
	 * Writing 0x12 to CHIP_AUDIO_RST (0x00) triggers an immediate soft
	 * reset before the I2C ACK/STOP completes, so -ENXIO / -EREMOTEIO
	 * is expected here.
	 */
	ret = i2c_master_send(client, write_cmd, 2);
	if (ret != 2 && ret != -ENXIO && ret != -EREMOTEIO) {
		dev_err(&client->dev, "%s: failed: %d\n", __func__, ret);
		return ret < 0 ? ret : -EIO;
	}

	usleep_range(2000, 5000);

	ret = ac108_read(CHIP_AUDIO_RST, &val, client);
	if (ret < 0)
		return ret;

	return 0;
}

static int ac108_update_bits(u8 reg, u8 mask, u8 value,
			     struct i2c_client *client)
{
	u8 val_old, val_new;
	int ret;

	if (!client)
		return -ENODEV;

	ret = ac108_read(reg, &val_old, client);
	if (ret < 0)
		return ret;

	val_new = (val_old & ~mask) | (value & mask);
	if (val_new != val_old)
		return ac108_write(reg, val_new, client);

	return 0;
}

static int ac108_multi_chips_update_bits(u8 reg, u8 mask, u8 value)
{
	unsigned int i;
	int ret;

	for (i = 0; i < ARRAY_SIZE(i2c_clt); i++) {
		if (!i2c_clt[i])
			continue;
		ret = ac108_update_bits(reg, mask, value, i2c_clt[i]);
		if (ret < 0)
			return ret;
	}

	return 0;
}

/**
 * snd_ac108_get_volsw - single mixer get callback
 * @kcontrol: mixer control
 * @ucontrol: control element information
 *
 * Callback to get the value of a single mixer control, or a double mixer
 * control that spans 2 registers.
 *
 * Returns 0 for success.
 */
static int snd_ac108_get_volsw(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_value *ucontrol)
{
	struct soc_mixer_control *mc =
		(struct soc_mixer_control *)kcontrol->private_value;
	unsigned int reg = mc->reg;
	unsigned int shift = mc->shift;
	int max = mc->max;
	int min = mc->min;
	unsigned int chip = mc->autodisable;
	unsigned int mask = (1 << fls(max)) - 1;
	unsigned int invert = mc->invert;
	u8 val;
	int ret;

	if (chip >= ARRAY_SIZE(i2c_clt))
		return -EINVAL;

	ret = ac108_read(reg, &val, i2c_clt[chip]);
	if (ret < 0)
		return ret;

	val = (val >> shift) & mask;

	ucontrol->value.integer.value[0] = val - min;
	if (invert)
		ucontrol->value.integer.value[0] =
			max - ucontrol->value.integer.value[0];

	return 0;
}

/**
 * snd_ac108_put_volsw - single mixer put callback
 * @kcontrol: mixer control
 * @ucontrol: control element information
 *
 * Callback to set the value of a single mixer control, or a double mixer
 * control that spans 2 registers.
 *
 * Returns 0 for success.
 */
static int snd_ac108_put_volsw(struct snd_kcontrol *kcontrol,
			       struct snd_ctl_elem_value *ucontrol)
{
	struct soc_mixer_control *mc =
		(struct soc_mixer_control *)kcontrol->private_value;
	unsigned int reg = mc->reg;
	unsigned int shift = mc->shift;
	int max = mc->max;
	int min = mc->min;
	unsigned int sign_bit = mc->sign_bit;
	unsigned int mask = (1 << fls(max)) - 1;
	unsigned int invert = mc->invert;
	unsigned int val, val_mask;
	unsigned int chip = mc->autodisable;

	if (chip >= ARRAY_SIZE(i2c_clt))
		return -EINVAL;

	if (sign_bit)
		mask = BIT(sign_bit + 1) - 1;

	val = ((ucontrol->value.integer.value[0] + min) & mask);
	if (invert)
		val = max - val;
	val_mask = mask << shift;
	val = val << shift;

	return ac108_update_bits(reg, val_mask, val, i2c_clt[chip]);
}
#define SOC_AC108_SINGLE_TLV(xname, reg, shift, max, invert, chip, tlv_array) \
{	.iface = SNDRV_CTL_ELEM_IFACE_MIXER, .name = xname, \
	.access = SNDRV_CTL_ELEM_ACCESS_TLV_READ |\
		 SNDRV_CTL_ELEM_ACCESS_READWRITE,\
	.tlv.p = (tlv_array), \
	.info = snd_soc_info_volsw, .get = snd_ac108_get_volsw,\
	.put = snd_ac108_put_volsw, \
	.private_value = SOC_SINGLE_VALUE(reg, shift, 0, max, invert, chip) }

static const struct snd_kcontrol_new ac108_volume_snd_controls[] = {
	/*0x70: ADC1 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH1 volume", ADC1_DVOL_CTRL, 0, 0xff, 0, 0, ch_digital_vol_tlv),
	/*0x71: ADC2 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH2 volume", ADC2_DVOL_CTRL, 0, 0xff, 0, 0, ch_digital_vol_tlv),
	/*0x72: ADC3 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH3 volume", ADC3_DVOL_CTRL, 0, 0xff, 0, 0, ch_digital_vol_tlv),
	/*0x73: ADC4 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH4 volume", ADC4_DVOL_CTRL, 0, 0xff, 0, 0, ch_digital_vol_tlv),

	/*0x90: Analog PGA1 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC1 PGA gain", ANA_PGA1_CTRL, ADC1_ANALOG_PGA, 0x1f, 0, 0, adc_pga_gain_tlv),
	/*0x91: Analog PGA2 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC2 PGA gain", ANA_PGA2_CTRL, ADC2_ANALOG_PGA, 0x1f, 0, 0, adc_pga_gain_tlv),
	/*0x92: Analog PGA3 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC3 PGA gain", ANA_PGA3_CTRL, ADC3_ANALOG_PGA, 0x1f, 0, 0, adc_pga_gain_tlv),
	/*0x93: Analog PGA4 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC4 PGA gain", ANA_PGA4_CTRL, ADC4_ANALOG_PGA, 0x1f, 0, 0, adc_pga_gain_tlv),

	/*0x70: ADC1 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH5 volume", ADC1_DVOL_CTRL, 0, 0xff, 0, 1, ch_digital_vol_tlv),
	/*0x71: ADC2 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH6 volume", ADC2_DVOL_CTRL, 0, 0xff, 0, 1, ch_digital_vol_tlv),
	/*0x72: ADC3 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH7 volume", ADC3_DVOL_CTRL, 0, 0xff, 0, 1, ch_digital_vol_tlv),
	/*0x73: ADC4 Digital Channel Volume Control Register*/
	SOC_AC108_SINGLE_TLV("CH8 volume", ADC4_DVOL_CTRL, 0, 0xff, 0, 1, ch_digital_vol_tlv),

	/*0x90: Analog PGA1 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC5 PGA gain", ANA_PGA1_CTRL, ADC1_ANALOG_PGA, 0x1f, 0, 1, adc_pga_gain_tlv),
	/*0x91: Analog PGA2 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC6 PGA gain", ANA_PGA2_CTRL, ADC2_ANALOG_PGA, 0x1f, 0, 1, adc_pga_gain_tlv),
	/*0x92: Analog PGA3 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC7 PGA gain", ANA_PGA3_CTRL, ADC3_ANALOG_PGA, 0x1f, 0, 1, adc_pga_gain_tlv),
	/*0x93: Analog PGA4 Control Register*/
	SOC_AC108_SINGLE_TLV("ADC8 PGA gain", ANA_PGA4_CTRL, ADC4_ANALOG_PGA, 0x1f, 0, 1, adc_pga_gain_tlv),
};



static int ac108_hw_init(struct i2c_client *i2c)
{
	int ret;

#if !AC108_DMIC_EN
	/* Analog voltage enable */
	ret = ac108_write(PWR_CTRL6, 0x01, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(PWR_CTRL7, 0x9b, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(PWR_CTRL9, 0x81, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ANA_ADC3_CTRL7, 0x03, i2c);
	if (ret < 0)
		return ret;
#endif

	/* SYSCLK Config */
	ret = ac108_update_bits(SYSCLK_CTRL, BIT(SYSCLK_EN), BIT(SYSCLK_EN),
				i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(MOD_CLK_EN, 0x93, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(MOD_RST_CTRL, 0x93, i2c);
	if (ret < 0)
		return ret;

	/* I2S Common Config */
	ret = ac108_update_bits(I2S_CTRL, BIT(SDO1_EN) | BIT(SDO2_EN),
				BIT(SDO1_EN) | (!!AC108_SDO2_EN << SDO2_EN),
				i2c);
	if (ret < 0)
		return ret;
	ret = ac108_update_bits(I2S_BCLK_CTRL, BIT(EDGE_TRANSFER), 0, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_update_bits(I2S_LRCK_CTRL1, 0x3 << LRCK_PERIODH,
				((AC108_LRCK_PERIOD - 1) >> 8) << LRCK_PERIODH,
				i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_LRCK_CTRL2, (u8)(AC108_LRCK_PERIOD - 1), i2c);
	if (ret < 0)
		return ret;
	ret = ac108_update_bits(I2S_FMT_CTRL1,
				BIT(ENCD_SEL) | BIT(TX_SLOT_HIZ) | BIT(TX_STATE),
				(!!AC108_ENCODING_EN << ENCD_SEL) | BIT(TX_STATE),
				i2c);
	if (ret < 0)
		return ret;
	ret = ac108_update_bits(I2S_FMT_CTRL2, 0x7 << SLOT_WIDTH_SEL,
				(AC108_SLOT_WIDTH == 16 ? 3 :
				 AC108_SLOT_WIDTH == 32 ? 7 : 0) << SLOT_WIDTH_SEL,
				i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_FMT_CTRL3, AC108_SDO2_EN ? 0x60 : 0x70, i2c);
	if (ret < 0)
		return ret;

	ret = ac108_write(I2S_TX1_CHMP_CTRL1, 0x04, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX1_CHMP_CTRL2, 0x00, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX1_CHMP_CTRL3, 0x00, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX1_CHMP_CTRL4, 0x00, i2c);
	if (ret < 0)
		return ret;
#if AC108_SDO2_EN
	ret = ac108_write(I2S_TX2_CHMP_CTRL1, 0x0e, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX2_CHMP_CTRL2, 0x00, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX2_CHMP_CTRL3, 0x00, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(I2S_TX2_CHMP_CTRL4, 0x00, i2c);
	if (ret < 0)
		return ret;
#endif

	/* ADC DIG part Config */
	ret = ac108_write(ADC_DIG_EN, 0x1f, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ANA_ADC4_CTRL7, 0x0f, i2c);
	if (ret < 0)
		return ret;

#if AC108_ADC_PATTERN_SEL
	ret = ac108_write(HPF_EN, 0x00, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ADC_DIG_DEBUG, AC108_ADC_PATTERN_SEL & 0x7, i2c);
	if (ret < 0)
		return ret;
#endif

#if !AC108_DMIC_EN
	/* enable AAF/ADC/PGA and UnMute Config */
	ret = ac108_write(ANA_ADC1_CTRL1, 0x07, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ANA_ADC2_CTRL1, 0x07, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ANA_ADC3_CTRL1, 0x07, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(ANA_ADC4_CTRL1, 0x07, i2c);
	if (ret < 0)
		return ret;

	msleep(50);
	ret = ac108_update_bits(PWR_CTRL7, BIT(VREF_FASTSTART_ENABLE), 0, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_update_bits(PWR_CTRL9, BIT(VREFP_FASTSTART_ENABLE), 0, i2c);
	if (ret < 0)
		return ret;
#else
	/* DMIC module Enable */
	ret = ac108_write(DMIC_EN, 0x03, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(GPIO_CFG1, 0xee, i2c);
	if (ret < 0)
		return ret;
	ret = ac108_write(GPIO_CFG2, 0x7e, i2c);
	if (ret < 0)
		return ret;
#endif
	return 0;
}


static int ac108_set_sysclk(struct snd_soc_dai *dai, int clk_id,
			    unsigned int freq, int dir)
{
	int ret;

	switch (clk_id) {
	case SYSCLK_SRC_MCLK:
		dev_dbg(dai->dev, "AC108 SYSCLK source select MCLK\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    BIT(SYSCLK_SRC), 0);
		break;
	case SYSCLK_SRC_PLL:
		dev_dbg(dai->dev, "AC108 SYSCLK source select PLL\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    BIT(SYSCLK_SRC),
						    BIT(SYSCLK_SRC));
		break;
	default:
		dev_err(dai->dev, "AC108 SYSCLK source config error: %d\n",
			clk_id);
		return -EINVAL;
	}
	if (ret < 0)
		return ret;

	return ac108_multi_chips_update_bits(SYSCLK_CTRL, BIT(SYSCLK_EN),
					     BIT(SYSCLK_EN));
}

static int ac108_set_pll(struct snd_soc_dai *dai, int pll_id, int source,
			 unsigned int freq_in, unsigned int freq_out)
{
	u32 i, m1 = 0, m2 = 0, n = 0, k1 = 0, k2 = 0;
	int ret;


	if (freq_in < 128000 || freq_in > 24576000) {
		dev_err(dai->dev,
			"AC108 PLLCLK input freq only supports [128K,24.576M], got %u\n",
			freq_in);
		return -EINVAL;
	} else if ((freq_in == 24576000 || freq_in == 22579200) &&
		   pll_id == PLLCLK_SRC_MCLK) {
		dev_dbg(dai->dev, "AC108 don't need to use PLL\n");
		return ac108_multi_chips_update_bits(SYSCLK_CTRL,
						     BIT(SYSCLK_SRC) | BIT(SYSCLK_EN),
						     BIT(SYSCLK_EN));
	}

	/* PLL Clock Source Select */
	switch (pll_id) {
	case PLLCLK_SRC_MCLK:
		dev_dbg(dai->dev, "AC108 PLLCLK input source select MCLK\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    0x3 << PLLCLK_SRC,
						    0x0 << PLLCLK_SRC);
		break;
	case PLLCLK_SRC_BCLK:
		dev_dbg(dai->dev, "AC108 PLLCLK input source select BCLK\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    0x3 << PLLCLK_SRC,
						    0x1 << PLLCLK_SRC);
		break;
	case PLLCLK_SRC_GPIO2:
		dev_dbg(dai->dev, "AC108 PLLCLK input source select GPIO2\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    0x3 << PLLCLK_SRC,
						    0x2 << PLLCLK_SRC);
		break;
	case PLLCLK_SRC_GPIO3:
		dev_dbg(dai->dev, "AC108 PLLCLK input source select GPIO3\n");
		ret = ac108_multi_chips_update_bits(SYSCLK_CTRL,
						    0x3 << PLLCLK_SRC,
						    0x3 << PLLCLK_SRC);
		break;
	default:
		dev_err(dai->dev, "AC108 PLLCLK source config error: %d\n",
			pll_id);
		return -EINVAL;
	}
	if (ret < 0)
		return ret;

	/* FOUT = (FIN * N) / [(M1+1) * (M2+1) * (K1+1) * (K2+1)] */
	for (i = 0; i < ARRAY_SIZE(ac108_pll_div); i++) {
		if (ac108_pll_div[i].freq_in == freq_in) {
			m1 = ac108_pll_div[i].m1;
			m2 = ac108_pll_div[i].m2;
			n = ac108_pll_div[i].n;
			k1 = ac108_pll_div[i].k1;
			k2 = ac108_pll_div[i].k2;
			dev_dbg(dai->dev,
				"AC108 PLL freq_in match: %u, freq_out: %u\n",
				freq_in, ac108_pll_div[i].freq_out);
			break;
		}
	}

	if (i == ARRAY_SIZE(ac108_pll_div)) {
		dev_err(dai->dev,
			"AC108 don't match PLLCLK freq_in and freq_out table\n");
		return -EINVAL;
	}

	/* Config PLL DIV param M1/M2/N/K1/K2 */
	ret = ac108_multi_chips_update_bits(PLL_CTRL2,
					    0x1f << PLL_PREDIV1 | BIT(PLL_PREDIV2),
					    m1 << PLL_PREDIV1 | m2 << PLL_PREDIV2);
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(PLL_CTRL3, 0x3 << PLL_LOOPDIV_MSB,
					    (n >> 8) << PLL_LOOPDIV_MSB);
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(PLL_CTRL4, 0xff << PLL_LOOPDIV_LSB,
					    (u8)n << PLL_LOOPDIV_LSB);
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(PLL_CTRL5,
					    0x1f << PLL_POSTDIV1 | BIT(PLL_POSTDIV2),
					    k1 << PLL_POSTDIV1 | k2 << PLL_POSTDIV2);
	if (ret < 0)
		return ret;

	/* Config PLL module current */
	ret = ac108_multi_chips_update_bits(PLL_CTRL1, 0x7 << PLL_IBIAS, 0);
	if (ret < 0)
		return ret;

	/* PLL module enable */
	ret = ac108_multi_chips_update_bits(PLL_LOCK_CTRL, BIT(PLL_LOCK_EN),
					    BIT(PLL_LOCK_EN));
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(PLL_CTRL1,
					    BIT(PLL_EN) | BIT(PLL_COM_EN),
					    BIT(PLL_EN) | BIT(PLL_COM_EN));
	if (ret < 0)
		return ret;

	/* PLLCLK Enable, System Clock Source Select PLL, SYSCLK Enable */
	return ac108_multi_chips_update_bits(SYSCLK_CTRL,
					     BIT(PLLCLK_EN) | BIT(SYSCLK_SRC) | BIT(SYSCLK_EN),
					     BIT(PLLCLK_EN) | BIT(SYSCLK_SRC) | BIT(SYSCLK_EN));
}

static int ac108_set_clkdiv(struct snd_soc_dai *dai, int div_id, int div)
{
	u32 i, bclk_div, bclk_div_reg_val = 0;


	if (!div_id) {	/* 0: Slave mode, 1: Master mode */
		dev_dbg(dai->dev,
			"AC108 work as Slave mode, don't need BCLK_DIV\n");
		return 0;
	}

	bclk_div = div / AC108_LRCK_PERIOD;

	for (i = 0; i < ARRAY_SIZE(ac108_bclk_div); i++) {
		if (ac108_bclk_div[i].real_val == bclk_div) {
			bclk_div_reg_val = ac108_bclk_div[i].reg_val;
			dev_dbg(dai->dev, "AC108 set BCLK_DIV_[%u]\n",
				bclk_div);
			break;
		}
	}

	if (i == ARRAY_SIZE(ac108_bclk_div)) {
		dev_err(dai->dev, "AC108 don't support BCLK_DIV_[%u]\n",
			bclk_div);
		return -EINVAL;
	}

	return ac108_multi_chips_update_bits(I2S_BCLK_CTRL, 0xf << BCLKDIV,
					     bclk_div_reg_val << BCLKDIV);
}

static int ac108_set_fmt(struct snd_soc_dai *dai, unsigned int fmt)
{
	u8 tx_offset, i2s_mode, lrck_polarity, brck_polarity;
	struct ac108_priv *ac108 = dev_get_drvdata(dai->dev);
	unsigned int i;
	int ret;

	/* AC108 config Master/Slave mode */
	switch (fmt & SND_SOC_DAIFMT_MASTER_MASK) {
	case SND_SOC_DAIFMT_CBP_CFP:
		dev_dbg(dai->dev, "AC108 set to work as Master\n");
		ret = ac108_update_bits(I2S_CTRL, 0x3 << LRCK_IOEN,
					0x3 << LRCK_IOEN, ac108->i2c);
		break;
	case SND_SOC_DAIFMT_CBC_CFC:
		dev_dbg(dai->dev, "AC108 set to work as Slave\n");
		ret = ac108_update_bits(I2S_CTRL, 0x3 << LRCK_IOEN,
					0x0 << LRCK_IOEN, ac108->i2c);
		break;
	default:
		dev_err(dai->dev, "AC108 Master/Slave mode config error: %u\n",
			(fmt & SND_SOC_DAIFMT_MASTER_MASK) >> 12);
		return -EINVAL;
	}
	if (ret < 0)
		return ret;

	/* multi_chips: only one chip as Master, others set as Slave */
	for (i = 0; i < ARRAY_SIZE(i2c_clt); i++) {
		if (!i2c_clt[i] || i2c_clt[i] == ac108->i2c)
			continue;
		ret = ac108_update_bits(I2S_CTRL, 0x3 << LRCK_IOEN,
					0x0 << LRCK_IOEN, i2c_clt[i]);
		if (ret < 0)
			return ret;
	}

	/* AC108 config I2S/LJ/RJ/PCM format */
	switch (fmt & SND_SOC_DAIFMT_FORMAT_MASK) {
	case SND_SOC_DAIFMT_I2S:
		dev_dbg(dai->dev, "AC108 config I2S format\n");
		i2s_mode = LEFT_JUSTIFIED_FORMAT;
		tx_offset = 1;
		break;
	case SND_SOC_DAIFMT_RIGHT_J:
		dev_dbg(dai->dev, "AC108 config RIGHT-JUSTIFIED format\n");
		i2s_mode = RIGHT_JUSTIFIED_FORMAT;
		tx_offset = 0;
		break;
	case SND_SOC_DAIFMT_LEFT_J:
		dev_dbg(dai->dev, "AC108 config LEFT-JUSTIFIED format\n");
		i2s_mode = LEFT_JUSTIFIED_FORMAT;
		tx_offset = 0;
		break;
	case SND_SOC_DAIFMT_DSP_A:
		dev_dbg(dai->dev, "AC108 config PCM-A format\n");
		i2s_mode = PCM_FORMAT;
		tx_offset = 1;
		break;
	case SND_SOC_DAIFMT_DSP_B:
		dev_dbg(dai->dev, "AC108 config PCM-B format\n");
		i2s_mode = PCM_FORMAT;
		tx_offset = 0;
		break;
	default:
		dev_err(dai->dev, "AC108 I2S format config error: %u\n",
			fmt & SND_SOC_DAIFMT_FORMAT_MASK);
		return -EINVAL;
	}
	ret = ac108_multi_chips_update_bits(I2S_FMT_CTRL1,
					    0x3 << MODE_SEL |
					    BIT(TX2_OFFSET) | BIT(TX1_OFFSET),
					    i2s_mode << MODE_SEL |
					    tx_offset << TX2_OFFSET |
					    tx_offset << TX1_OFFSET);
	if (ret < 0)
		return ret;

	/* AC108 config BCLK & LRCK polarity */
	switch (fmt & SND_SOC_DAIFMT_INV_MASK) {
	case SND_SOC_DAIFMT_NB_NF:
		dev_dbg(dai->dev, "AC108 BCLK&LRCK: BCLK_normal,LRCK_normal\n");
		brck_polarity = BCLK_NORMAL_DRIVE_N_SAMPLE_P;
		lrck_polarity = LRCK_LEFT_LOW_RIGHT_HIGH;
		break;
	case SND_SOC_DAIFMT_NB_IF:
		dev_dbg(dai->dev, "AC108 BCLK&LRCK: BCLK_normal,LRCK_invert\n");
		brck_polarity = BCLK_NORMAL_DRIVE_N_SAMPLE_P;
		lrck_polarity = LRCK_LEFT_HIGH_RIGHT_LOW;
		break;
	case SND_SOC_DAIFMT_IB_NF:
		dev_dbg(dai->dev, "AC108 BCLK&LRCK: BCLK_invert,LRCK_normal\n");
		brck_polarity = BCLK_INVERT_DRIVE_P_SAMPLE_N;
		lrck_polarity = LRCK_LEFT_LOW_RIGHT_HIGH;
		break;
	case SND_SOC_DAIFMT_IB_IF:
		dev_dbg(dai->dev, "AC108 BCLK&LRCK: BCLK_invert,LRCK_invert\n");
		brck_polarity = BCLK_INVERT_DRIVE_P_SAMPLE_N;
		lrck_polarity = LRCK_LEFT_HIGH_RIGHT_LOW;
		break;
	default:
		dev_err(dai->dev, "AC108 BCLK/LRCLK polarity error: %u\n",
			(fmt & SND_SOC_DAIFMT_INV_MASK) >> 8);
		return -EINVAL;
	}
	ret = ac108_multi_chips_update_bits(I2S_BCLK_CTRL, BIT(BCLK_POLARITY),
					    brck_polarity << BCLK_POLARITY);
	if (ret < 0)
		return ret;

	return ac108_multi_chips_update_bits(I2S_LRCK_CTRL1, BIT(LRCK_POLARITY),
					     lrck_polarity << LRCK_POLARITY);
}

static int ac108_hw_params(struct snd_pcm_substream *substream,
			   struct snd_pcm_hw_params *params,
			   struct snd_soc_dai *dai)
{
	unsigned int i, num_chips, sample_resolution;
	int ret;

	for (i = 0; i < ARRAY_SIZE(i2c_clt); i++) {
		if (!i2c_clt[i])
			continue;
		ret = ac108_hw_init(i2c_clt[i]);
		if (ret < 0)
			return ret;
	}

	/*
	 * Clocking: the chips are bit-clock slaves and derive their system clock
	 * from BCLK (64 * fs from the SoC I2S) through the on-chip PLL. The vendor
	 * 4.4 kernel did this from a patched simple-card; do it here instead so
	 * the stock machine driver can be used.
	 *
	 * rockchip_i2s defaults to bclk_ratio = 64 across 4 stereo I2S lanes,
	 * so BCLK is always params_rate(params) * 2 * 32 regardless of channels.
	 */
	ret = ac108_set_sysclk(dai, SYSCLK_SRC_PLL, 0, 0);
	if (ret < 0)
		return ret;
	ret = ac108_set_pll(dai, PLLCLK_SRC_BCLK, 0,
			    params_rate(params) * 2 * 32, 0);
	if (ret < 0)
		return ret;

	/* AC108 set sample rate */
	for (i = 0; i < ARRAY_SIZE(ac108_sample_rate); i++) {
		if (ac108_sample_rate[i].real_val ==
		    params_rate(params) /
		    (AC108_ENCODING_EN ? AC108_ENCODING_CH_NUMS / 2 : 1)) {
			ret = ac108_multi_chips_update_bits(ADC_SPRC,
							    0xf << ADC_FS_I2S1,
							    ac108_sample_rate[i].reg_val << ADC_FS_I2S1);
			if (ret < 0)
				return ret;
			break;
		}
	}
	if (i == ARRAY_SIZE(ac108_sample_rate)) {
		dev_err(dai->dev, "AC108 unsupported sample rate: %u\n",
			params_rate(params));
		return -EINVAL;
	}

	/* AC108 set channels: 4 channels per chip across SDO1 + SDO2 */
	num_chips = min_t(unsigned int, DIV_ROUND_UP(params_channels(params), 4),
			  ARRAY_SIZE(i2c_clt));
	dev_dbg(dai->dev, "channels=%u, chips=%u\n",
		params_channels(params), num_chips);
	for (i = 0; i < num_chips; i++) {
		if (!i2c_clt[i])
			return -ENODEV;
		ret = ac108_write(I2S_TX1_CTRL1, 0x01, i2c_clt[i]);
		if (ret < 0)
			return ret;
		ret = ac108_write(I2S_TX1_CTRL2, 0x03, i2c_clt[i]);
		if (ret < 0)
			return ret;
		ret = ac108_write(I2S_TX1_CTRL3, 0x00, i2c_clt[i]);
		if (ret < 0)
			return ret;
	}
	for (i = 0; i < num_chips; i++) {
		if (!i2c_clt[i])
			return -ENODEV;
		ret = ac108_write(I2S_TX2_CTRL1, 0x01, i2c_clt[i]);
		if (ret < 0)
			return ret;
		ret = ac108_write(I2S_TX2_CTRL2, 0x03, i2c_clt[i]);
		if (ret < 0)
			return ret;
		ret = ac108_write(I2S_TX2_CTRL3, 0x00, i2c_clt[i]);
		if (ret < 0)
			return ret;
	}

	/* AC108 set sample resolution */
	switch (params_format(params)) {
	case SNDRV_PCM_FORMAT_S8:
		sample_resolution = 8;
		break;
	case SNDRV_PCM_FORMAT_S16_LE:
		sample_resolution = 16;
		break;
	case SNDRV_PCM_FORMAT_S20_3LE:
		sample_resolution = 20;
		break;
	case SNDRV_PCM_FORMAT_S24_LE:
		sample_resolution = 24;
		break;
	case SNDRV_PCM_FORMAT_S32_LE:
		sample_resolution = 32;
		break;
	default:
		dev_err(dai->dev,
			"AC108 don't supported the sample resolution: %d\n",
			params_format(params));
		return -EINVAL;
	}

	for (i = 0; i < ARRAY_SIZE(ac108_sample_resolution); i++) {
		if (ac108_sample_resolution[i].real_val == sample_resolution) {
			ret = ac108_multi_chips_update_bits(I2S_FMT_CTRL2,
							    0x7 << SAMPLE_RESOLUTION,
							    ac108_sample_resolution[i].reg_val << SAMPLE_RESOLUTION);
			if (ret < 0)
				return ret;
			break;
		}
	}

	/* AC108 TX enable, Global enable */
	return ac108_multi_chips_update_bits(I2S_CTRL, BIT(TXEN) | BIT(GEN),
					     BIT(TXEN) | BIT(GEN));
}

static int ac108_hw_free(struct snd_pcm_substream *substream,
			 struct snd_soc_dai *dai)
{
	int ret;


#if !AC108_IDLE_RESET_EN
	/* repair PLL version no sync problem */
	ret = ac108_multi_chips_update_bits(PLL_CTRL1,
					    BIT(PLL_EN) | BIT(PLL_COM_EN), 0);
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(MOD_CLK_EN, BIT(ADC_DIGITAL), 0);
	if (ret < 0)
		return ret;
	ret = ac108_multi_chips_update_bits(MOD_RST_CTRL, BIT(ADC_DIGITAL), 0);
	if (ret < 0)
		return ret;

	/* AC108 I2S Global disable */
	ret = ac108_multi_chips_update_bits(I2S_CTRL, BIT(GEN), 0);
	if (ret < 0)
		return ret;
#endif

	return 0;
}


/*** define  ac108  dai_ops  struct ***/
static const struct snd_soc_dai_ops ac108_dai_ops = {
	/*DAI clocking configuration*/
	.set_sysclk = ac108_set_sysclk,
	.set_pll = ac108_set_pll,
	.set_clkdiv = ac108_set_clkdiv,

	/*ALSA PCM audio operations*/
	.hw_params = ac108_hw_params,
	.hw_free = ac108_hw_free,

	/*DAI format configuration*/
	.set_fmt = ac108_set_fmt,
};

static struct snd_soc_dai_driver ac108_dai0 = {
	.name = "ac108-pcm0",
	.capture = {
		.stream_name = "Capture",
		.channels_min = 1,
		.channels_max = AC108_CHANNELS_MAX,
		.rates = AC108_RATES,
		.formats = AC108_FORMATS,
	},
	.ops = &ac108_dai_ops,
};

static int ac108_comp_resume(struct snd_soc_component *component)
{
	unsigned int i;
	int ret;

	for (i = 0; i < ARRAY_SIZE(i2c_clt); i++) {
		if (!i2c_clt[i])
			continue;
		ret = ac108_hw_init(i2c_clt[i]);
		if (ret < 0)
			return ret;
	}
	return 0;
}

static const struct snd_soc_component_driver ac108_soc_codec_driver = {
	.resume = ac108_comp_resume,
	.controls = ac108_volume_snd_controls,
	.num_controls = ARRAY_SIZE(ac108_volume_snd_controls),
	.idle_bias_on = 1,
	.use_pmdown_time = 1,
	.endianness = 1,
};

static ssize_t ac108_store(struct device *dev, struct device_attribute *attr,
			   const char *buf, size_t count)
{
	struct ac108_priv *ac108 = dev_get_drvdata(dev);
	unsigned int val, flag;
	u8 i = 0, reg, num, value_w, value_r;
	int ret;

	ret = kstrtouint(buf, 16, &val);
	if (ret)
		return ret;
	if (val > 0xffffff)
		return -EINVAL;

	flag = (val >> 16) & 0xff;
	if (flag) {
		reg = (val >> 8) & 0xff;
		value_w = val & 0xff;
		dev_info(dev, "Write: start REG:0x%02x, val:0x%02x, count:0x%02x\n",
			 reg, value_w, flag);
		while (flag--) {
			ret = ac108_write(reg, value_w, ac108->i2c);
			if (ret < 0)
				return ret;
			dev_info(dev, "Write 0x%02x to REG:0x%02x\n",
				 value_w, reg);
			reg++;
		}
	} else {
		reg = (val >> 8) & 0xff;
		num = val & 0xff;
		if (!num)
			return -EINVAL;
		dev_info(dev, "Read: start REG:0x%02x, count:0x%02x\n",
			 reg, num);
		do {
			value_r = 0;
			ret = ac108_read(reg, &value_r, ac108->i2c);
			if (ret < 0)
				return ret;
			dev_info(dev, "REG[0x%02x]: 0x%02x\n", reg, value_r);
			reg++;
			i++;
		} while (i < num);
	}

	return count;
}

static ssize_t ac108_show(struct device *dev, struct device_attribute *attr,
			  char *buf)
{
	return sysfs_emit(buf,
			  "echo flag|reg|val > ac108\n"
			  "eg read start address=0x06, count 0x10: echo 0610 > ac108\n"
			  "eg write start address=0x90, value=0x3c, count=4: echo 4903c > ac108\n");
}

static DEVICE_ATTR_RW(ac108);

static struct attribute *ac108_debug_attrs[] = {
	&dev_attr_ac108.attr,
	NULL,
};
ATTRIBUTE_GROUPS(ac108_debug);

static const unsigned int ac108_chip_idx[] = { 0, 1 };

static const struct of_device_id ac108_dt_ids[] = {
	{ .compatible = "x-powers,ac108-micarray-0", .data = &ac108_chip_idx[0] },
	{ .compatible = "x-powers,ac108-micarray-1", .data = &ac108_chip_idx[1] },
	{ }
};
MODULE_DEVICE_TABLE(of, ac108_dt_ids);

static int ac108_i2c_probe(struct i2c_client *i2c)
{
	const unsigned int *chip_idx = i2c_get_match_data(i2c);
	struct ac108_priv *ac108;
	unsigned int idx;
	int ret;

	if (!chip_idx || *chip_idx >= ARRAY_SIZE(i2c_clt))
		return -EINVAL;
	idx = *chip_idx;

	ac108 = devm_kzalloc(&i2c->dev, sizeof(*ac108), GFP_KERNEL);
	if (!ac108)
		return -ENOMEM;
	ac108->i2c = i2c;
	i2c_set_clientdata(i2c, ac108);

	ret = ac108_soft_reset(i2c);
	if (ret < 0)
		return ret;

	ret = ac108_hw_init(i2c);
	if (ret < 0)
		return ret;

	i2c_clt[idx] = i2c;

	/*
	 * Only chip 0 is a sound component: its DAI ops drive every chip on the
	 * bus through i2c_clt[], as in the vendor 4.4 driver. Chip 1 just needs
	 * to be probed first-or-later and initialised.
	 */
	if (idx == 0) {
		ret = devm_snd_soc_register_component(&i2c->dev,
						      &ac108_soc_codec_driver,
						      &ac108_dai0, 1);
		if (ret < 0) {
			i2c_clt[idx] = NULL;
			dev_err(&i2c->dev,
				"Failed to register ac108 codec: %d\n", ret);
			return ret;
		}
	}

	dev_info(&i2c->dev, "ac108 chip %u ready\n", idx);
	return 0;
}

static void ac108_i2c_remove(struct i2c_client *i2c)
{
	const unsigned int *chip_idx = i2c_get_match_data(i2c);

	if (chip_idx && *chip_idx < ARRAY_SIZE(i2c_clt))
		i2c_clt[*chip_idx] = NULL;
}

static struct i2c_driver ac108_i2c_driver = {
	.driver = {
		.name = "ac108",
		.of_match_table = ac108_dt_ids,
		.dev_groups = ac108_debug_groups,
	},
	.probe = ac108_i2c_probe,
	.remove = ac108_i2c_remove,
};
module_i2c_driver(ac108_i2c_driver);

MODULE_DESCRIPTION("ASoC ac108 codec driver (ReSpeaker Core v2, ported from the Seeed 4.4 kernel)");
MODULE_AUTHOR("panjunwen");
MODULE_LICENSE("GPL");
